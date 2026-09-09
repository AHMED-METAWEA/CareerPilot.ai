"""Job source adapters.

One module per source. Each owns its container shape and its field vocabulary;
there is no shared response accessor, by rule (Appendix B).
"""

from app.adapters.sources import (  # noqa: F401
    ashby,
    greenhouse,
    lever,
    recruitee,
    smartrecruiters,
    workable,
)
from app.adapters.sources.base import (
    ADAPTERS,
    BaseSourceAdapter,
    build_adapter,
    register,
)

__all__ = ["ADAPTERS", "BaseSourceAdapter", "build_adapter", "register"]
