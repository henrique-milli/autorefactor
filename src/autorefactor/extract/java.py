from __future__ import annotations

import re
from pathlib import Path

import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

from autorefactor.extract.base import iter_source_files, make_unit
from autorefactor.models import CodeUnit, UnitKind

JAVA_LANGUAGE = Language(tsjava.language())


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _identifier(node: Node, source: bytes) -> str | None:
    for child in node.children:
        if child.type == "identifier":
            return _node_text(source, child)
    return None


def _package_name(root: Node, source: bytes) -> str:
    for child in root.children:
        if child.type == "package_declaration":
            text = _node_text(source, child)
            match = re.search(r"package\s+([\w.]+)\s*;", text)
            if match:
                return match.group(1)
    return ""


def _walk_types(node: Node) -> list[Node]:
    types: list[Node] = []
    if node.type in {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration"}:
        types.append(node)
    for child in node.children:
        types.extend(_walk_types(child))
    return types


class JavaExtractor:
    language = "java"
    suffixes = (".java",)

    def extract(self, repo_name: str, repo_path: Path) -> list[CodeUnit]:
        parser = Parser(JAVA_LANGUAGE)
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
        tree = parser.parse(source)
        package = _package_name(tree.root_node, source)
        text = source.decode("utf-8", errors="replace")
        module_name = package or rel.rsplit("/", 1)[0] or repo_name
        module = make_unit(
            repo=repo_name,
            path=rel,
            qualified_name=module_name,
            kind=UnitKind.MODULE,
            language=self.language,
            snippet=f"package {module_name}",
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
        for type_node in _walk_types(tree.root_node):
            name = _identifier(type_node, source)
            if not name:
                continue
            qualified = f"{package}.{name}" if package else name
            type_unit = make_unit(
                repo=repo_name,
                path=rel,
                qualified_name=qualified,
                kind=UnitKind.TYPE,
                language=self.language,
                snippet=_node_text(source, type_node),
                parent_id=file_unit.id,
                start_line=type_node.start_point[0] + 1,
                end_line=type_node.end_point[0] + 1,
                start_byte=type_node.start_byte,
                end_byte=type_node.end_byte,
            )
            units.append(type_unit)
            body = type_node.child_by_field_name("body")
            if not body:
                continue
            for child in body.children:
                if child.type not in {"method_declaration", "constructor_declaration"}:
                    continue
                method_name = _identifier(child, source) or "unknown"
                units.append(
                    make_unit(
                        repo=repo_name,
                        path=rel,
                        qualified_name=f"{qualified}.{method_name}",
                        kind=UnitKind.FUNCTION,
                        language=self.language,
                        snippet=_node_text(source, child),
                        parent_id=type_unit.id,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        start_byte=child.start_byte,
                        end_byte=child.end_byte,
                    )
                )
        return units
