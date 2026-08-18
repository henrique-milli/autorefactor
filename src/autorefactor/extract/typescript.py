from __future__ import annotations

from pathlib import Path

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from autorefactor.extract.base import iter_source_files, make_unit
from autorefactor.models import CodeUnit, UnitKind

TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _name_from_node(node: Node, source: bytes) -> str | None:
    name = node.child_by_field_name("name")
    if name is not None:
        return _node_text(source, name)
    for child in node.children:
        if child.type in {"identifier", "type_identifier", "property_identifier"}:
            return _node_text(source, child)
    return None


class TypeScriptExtractor:
    language = "typescript"
    suffixes = (".ts", ".tsx", ".js", ".jsx")

    def extract(self, repo_name: str, repo_path: Path) -> list[CodeUnit]:
        units: list[CodeUnit] = []
        for file_path in iter_source_files(repo_path, self.suffixes):
            lang = TSX_LANGUAGE if file_path.suffix.lower() in {".tsx", ".jsx"} else TS_LANGUAGE
            parser = Parser(lang)
            units.extend(self._extract_file(parser, repo_name, repo_path, file_path))
        return units

    def _extract_file(
        self,
        parser: Parser,
        repo_name: str,
        repo_path: Path,
        file_path: Path,
    ) -> list[CodeUnit]:
        try:
            source = file_path.read_bytes()
        except OSError:
            return []
        rel = str(file_path.relative_to(repo_path))
        text = source.decode("utf-8", errors="replace")
        module_name = rel.replace("/", ".").rsplit(".", 1)[0]
        module = make_unit(
            repo=repo_name,
            path=rel,
            qualified_name=module_name,
            kind=UnitKind.MODULE,
            language=self.language,
            snippet=f"module {module_name}",
        )
        file_unit = make_unit(
            repo=repo_name,
            path=rel,
            qualified_name=rel,
            kind=UnitKind.FILE,
            language=self.language,
            snippet=text,
            parent_id=module.id,
            end_line=text.count("\n") + 1,
        )
        units: list[CodeUnit] = [module, file_unit]
        tree = parser.parse(source)
        self._walk(tree.root_node, source, repo_name, rel, file_unit.id, module_name, units)
        return units

    def _walk(
        self,
        node: Node,
        source: bytes,
        repo: str,
        rel: str,
        parent_id: str,
        prefix: str,
        units: list[CodeUnit],
    ) -> None:
        interesting = {
            "class_declaration",
            "interface_declaration",
            "enum_declaration",
            "type_alias_declaration",
            "function_declaration",
            "method_definition",
            "generator_function_declaration",
        }
        for child in node.children:
            if child.type in interesting:
                name = _name_from_node(child, source) or "anonymous"
                qualified = f"{prefix}.{name}"
                kind = (
                    UnitKind.FUNCTION
                    if child.type
                    in {"function_declaration", "method_definition", "generator_function_declaration"}
                    else UnitKind.TYPE
                )
                unit = make_unit(
                    repo=repo,
                    path=rel,
                    qualified_name=qualified,
                    kind=kind,
                    language=self.language,
                    snippet=_node_text(source, child),
                    parent_id=parent_id,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    start_byte=child.start_byte,
                    end_byte=child.end_byte,
                )
                units.append(unit)
                if kind == UnitKind.TYPE:
                    self._walk(child, source, repo, rel, unit.id, qualified, units)
            elif child.type == "lexical_declaration":
                # const foo = () => {} / function expressions
                for decl in child.children:
                    if decl.type != "variable_declarator":
                        continue
                    name = _name_from_node(decl, source) or "anonymous"
                    value = decl.child_by_field_name("value")
                    if value is None or value.type not in {
                        "arrow_function",
                        "function_expression",
                        "generator_function",
                    }:
                        continue
                    units.append(
                        make_unit(
                            repo=repo,
                            path=rel,
                            qualified_name=f"{prefix}.{name}",
                            kind=UnitKind.FUNCTION,
                            language=self.language,
                            snippet=_node_text(source, child),
                            parent_id=parent_id,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            start_byte=child.start_byte,
                            end_byte=child.end_byte,
                        )
                    )
            else:
                self._walk(child, source, repo, rel, parent_id, prefix, units)
