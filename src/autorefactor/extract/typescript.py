from __future__ import annotations

import re
from pathlib import Path

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Node, Parser

from autorefactor.extract.base import iter_source_files, make_unit
from autorefactor.models import CodeUnit, UnitKind

TS_LANGUAGE = Language(tstypescript.language_typescript())
TSX_LANGUAGE = Language(tstypescript.language_tsx())

TSX_SUFFIXES = frozenset({".tsx", ".jsx"})

_HOOK_NAME = re.compile(r"^use[A-Z0-9]")
_PASCAL_NAME = re.compile(r"^[A-Z][A-Za-z0-9]*$")
# Cheap JSX markers: closing/self-closing tags or a returned/assigned JSX literal.
_JSX_MARKER = re.compile(r"</[A-Za-z]|/>|=>\s*<|return\s*<")


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


def _react_role(name: str, snippet: str, *, is_tsx: bool) -> str:
    """Tag React hooks and JSX-returning components (name/convention based)."""
    if _HOOK_NAME.match(name):
        return "react_hook"
    if is_tsx and _PASCAL_NAME.match(name) and _JSX_MARKER.search(snippet):
        return "react_component"
    return ""


class TypeScriptExtractor:
    language = "typescript"
    suffixes = (".ts", ".tsx", ".js", ".jsx")

    @staticmethod
    def _language_for(file_path: Path) -> str:
        return "tsx" if file_path.suffix.lower() in TSX_SUFFIXES else "typescript"

    def extract(self, repo_name: str, repo_path: Path) -> list[CodeUnit]:
        units: list[CodeUnit] = []
        for file_path in iter_source_files(repo_path, self.suffixes):
            lang = self._language_for(file_path)
            parser = Parser(TSX_LANGUAGE if lang == "tsx" else TS_LANGUAGE)
            units.extend(
                self._extract_file(parser, lang, repo_name, repo_path, file_path)
            )
        return units

    def _extract_file(
        self,
        parser: Parser,
        language: str,
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
            language=language,
            snippet=f"module {module_name}",
        )
        file_unit = make_unit(
            repo=repo_name,
            path=rel,
            qualified_name=rel,
            kind=UnitKind.FILE,
            language=language,
            snippet=text,
            parent_id=module.id,
            end_line=text.count("\n") + 1,
        )
        units: list[CodeUnit] = [module, file_unit]
        tree = parser.parse(source)
        self._walk(tree.root_node, source, repo_name, rel, file_unit.id, module_name, units, language)
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
        language: str,
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
        is_tsx = language == "tsx"
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
                snippet = _node_text(source, child)
                unit = make_unit(
                    repo=repo,
                    path=rel,
                    qualified_name=qualified,
                    kind=kind,
                    language=language,
                    snippet=snippet,
                    parent_id=parent_id,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    start_byte=child.start_byte,
                    end_byte=child.end_byte,
                    role=_react_role(name, snippet, is_tsx=is_tsx) if kind == UnitKind.FUNCTION else "",
                )
                units.append(unit)
                if kind == UnitKind.TYPE:
                    self._walk(child, source, repo, rel, unit.id, qualified, units, language)
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
                    snippet = _node_text(source, child)
                    units.append(
                        make_unit(
                            repo=repo,
                            path=rel,
                            qualified_name=f"{prefix}.{name}",
                            kind=UnitKind.FUNCTION,
                            language=language,
                            snippet=snippet,
                            parent_id=parent_id,
                            start_line=child.start_point[0] + 1,
                            end_line=child.end_point[0] + 1,
                            start_byte=child.start_byte,
                            end_byte=child.end_byte,
                            role=_react_role(name, snippet, is_tsx=is_tsx),
                        )
                    )
            else:
                self._walk(child, source, repo, rel, parent_id, prefix, units, language)
