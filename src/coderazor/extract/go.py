from __future__ import annotations

from pathlib import Path

import tree_sitter_go as tsgo
from tree_sitter import Language, Node, Parser

from coderazor.extract.base import iter_source_files, make_unit
from coderazor.models import CodeUnit, UnitKind

GO_LANGUAGE = Language(tsgo.language())


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _child_by_type(node: Node, type_name: str) -> Node | None:
    for child in node.children:
        if child.type == type_name:
            return child
    return None


class GoExtractor:
    language = "go"
    suffixes = (".go",)

    def extract(self, repo_name: str, repo_path: Path) -> list[CodeUnit]:
        parser = Parser(GO_LANGUAGE)
        units: list[CodeUnit] = []
        for file_path in iter_source_files(repo_path, self.suffixes):
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
        tree = parser.parse(source)
        package = "main"
        for child in tree.root_node.children:
            if child.type == "package_clause":
                ident = _child_by_type(child, "package_identifier")
                if ident:
                    package = _node_text(source, ident)
                break
        module = make_unit(
            repo=repo_name,
            path=rel,
            qualified_name=package,
            kind=UnitKind.MODULE,
            language=self.language,
            snippet=f"package {package}",
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
        for child in tree.root_node.children:
            if child.type == "type_declaration":
                for spec in child.children:
                    if spec.type != "type_spec":
                        continue
                    name_node = spec.child_by_field_name("name")
                    if not name_node:
                        continue
                    name = _node_text(source, name_node)
                    units.append(
                        make_unit(
                            repo=repo_name,
                            path=rel,
                            qualified_name=f"{package}.{name}",
                            kind=UnitKind.TYPE,
                            language=self.language,
                            snippet=_node_text(source, spec),
                            parent_id=file_unit.id,
                            start_line=spec.start_point[0] + 1,
                            end_line=spec.end_point[0] + 1,
                            start_byte=spec.start_byte,
                            end_byte=spec.end_byte,
                        )
                    )
            elif child.type == "function_declaration" or child.type == "method_declaration":
                name_node = child.child_by_field_name("name")
                name = _node_text(source, name_node) if name_node else "anonymous"
                units.append(
                    make_unit(
                        repo=repo_name,
                        path=rel,
                        qualified_name=f"{package}.{name}",
                        kind=UnitKind.FUNCTION,
                        language=self.language,
                        snippet=_node_text(source, child),
                        parent_id=file_unit.id,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                    )
                )
        return units
