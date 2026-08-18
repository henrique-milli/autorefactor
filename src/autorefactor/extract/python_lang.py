from __future__ import annotations

from pathlib import Path

import tree_sitter_python as tspython
from tree_sitter import Language, Node, Parser

from autorefactor.extract.base import iter_source_files, make_unit
from autorefactor.models import CodeUnit, UnitKind

PY_LANGUAGE = Language(tspython.language())


def _node_text(source: bytes, node: Node) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _name(node: Node, source: bytes) -> str | None:
    name = node.child_by_field_name("name")
    if name is None:
        return None
    return _node_text(source, name)


class PythonExtractor:
    language = "python"
    suffixes = (".py",)

    def extract(self, repo_name: str, repo_path: Path) -> list[CodeUnit]:
        parser = Parser(PY_LANGUAGE)
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
        module_name = rel.replace("/", ".").removesuffix(".py")
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
        for child in node.children:
            if child.type == "class_definition":
                name = _name(child, source) or "Anonymous"
                qualified = f"{prefix}.{name}"
                type_unit = make_unit(
                    repo=repo,
                    path=rel,
                    qualified_name=qualified,
                    kind=UnitKind.TYPE,
                    language=self.language,
                    snippet=_node_text(source, child),
                    parent_id=parent_id,
                    start_line=child.start_point[0] + 1,
                    end_line=child.end_point[0] + 1,
                    start_byte=child.start_byte,
                    end_byte=child.end_byte,
                )
                units.append(type_unit)
                body = child.child_by_field_name("body")
                if body:
                    self._walk(body, source, repo, rel, type_unit.id, qualified, units)
            elif child.type == "function_definition":
                name = _name(child, source) or "anonymous"
                qualified = f"{prefix}.{name}"
                units.append(
                    make_unit(
                        repo=repo,
                        path=rel,
                        qualified_name=qualified,
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
