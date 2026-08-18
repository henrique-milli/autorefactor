from __future__ import annotations

import re
from pathlib import Path

from autorefactor.extract.base import iter_source_files, make_unit, truncate
from autorefactor.models import CodeUnit, UnitKind

_MESSAGE_RE = re.compile(r"message\s+(\w+)\s*\{", re.MULTILINE)
_SERVICE_RE = re.compile(r"service\s+(\w+)\s*\{", re.MULTILINE)
_RPC_RE = re.compile(
    r"rpc\s+(\w+)\s*\([^)]*\)\s*returns\s*\([^)]*\)\s*;?",
    re.MULTILINE,
)


def _extract_block(text: str, start: int) -> str:
    depth = 0
    i = start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return text[start:]


class ProtoExtractor:
    language = "protobuf"
    suffixes = (".proto",)

    def extract(self, repo_name: str, repo_path: Path) -> list[CodeUnit]:
        units: list[CodeUnit] = []
        for file_path in iter_source_files(repo_path, self.suffixes):
            units.extend(self._extract_file(repo_name, repo_path, file_path))
        return units

    def _extract_file(self, repo_name: str, repo_path: Path, file_path: Path) -> list[CodeUnit]:
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        rel = str(file_path.relative_to(repo_path))
        module = make_unit(
            repo=repo_name,
            path=rel,
            qualified_name=file_path.stem,
            kind=UnitKind.MODULE,
            language=self.language,
            snippet=f"proto {file_path.stem}",
        )
        file_unit = make_unit(
            repo=repo_name,
            path=rel,
            qualified_name=rel,
            kind=UnitKind.FILE,
            language=self.language,
            snippet=truncate(text),
            parent_id=module.id,
            end_line=text.count("\n") + 1,
        )
        units: list[CodeUnit] = [module, file_unit]

        for match in _MESSAGE_RE.finditer(text):
            name = match.group(1)
            block = _extract_block(text, match.start())
            units.append(
                make_unit(
                    repo=repo_name,
                    path=rel,
                    qualified_name=f"{file_path.stem}.{name}",
                    kind=UnitKind.PROTO_MESSAGE,
                    language=self.language,
                    snippet=block,
                    parent_id=file_unit.id,
                )
            )

        for match in _SERVICE_RE.finditer(text):
            name = match.group(1)
            block = _extract_block(text, match.start())
            service = make_unit(
                repo=repo_name,
                path=rel,
                qualified_name=f"{file_path.stem}.{name}",
                kind=UnitKind.PROTO_SERVICE,
                language=self.language,
                snippet=block,
                parent_id=file_unit.id,
            )
            units.append(service)
            for rpc in _RPC_RE.finditer(block):
                rpc_name = rpc.group(1)
                units.append(
                    make_unit(
                        repo=repo_name,
                        path=rel,
                        qualified_name=f"{file_path.stem}.{name}.{rpc_name}",
                        kind=UnitKind.PROTO_RPC,
                        language=self.language,
                        snippet=rpc.group(0),
                        parent_id=service.id,
                    )
                )
        return units
