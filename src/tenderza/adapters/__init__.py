# Import built-in adapter modules for their @register_adapter side effects,
# so `import tenderza.adapters` is enough to populate the registry (the
# scheduler resolves adapters by key at runtime).
from tenderza.adapters import generic_cms as _generic_cms  # noqa: F401, E402
from tenderza.adapters import generic_sitemap_rss as _generic_sitemap_rss  # noqa: F401, E402
from tenderza.adapters import ocds_api as _ocds_api  # noqa: F401, E402
from tenderza.adapters import pdf_bulletin as _pdf_bulletin  # noqa: F401, E402
from tenderza.adapters.base import (
    Adapter,
    AdapterResult,
    RawTenderNotice,
    SourceConfig,
    get_adapter,
    register_adapter,
)

__all__ = [
    "Adapter",
    "AdapterResult",
    "RawTenderNotice",
    "SourceConfig",
    "get_adapter",
    "register_adapter",
]
