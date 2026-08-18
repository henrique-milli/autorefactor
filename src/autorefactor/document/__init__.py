from autorefactor.document.detect import (
    find_document_gaps,
    is_replaceable_doc,
    looks_documented,
)
from autorefactor.document.packs import AUTOREFACTOR_DOC_MARKER, get_pack

__all__ = [
    "AUTOREFACTOR_DOC_MARKER",
    "find_document_gaps",
    "get_pack",
    "is_replaceable_doc",
    "looks_documented",
]
