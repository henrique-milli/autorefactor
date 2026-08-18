from autorefactor.enrich.describe import describe_units
from autorefactor.enrich.embed import embed_units

__all__ = ["attach_text_facets", "describe_units", "embed_units", "enrich_coverage"]


def __getattr__(name: str):
    if name == "enrich_coverage":
        from autorefactor.enrich.coverage import enrich_coverage

        return enrich_coverage
    if name == "attach_text_facets":
        from autorefactor.enrich.facets import attach_text_facets

        return attach_text_facets
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
