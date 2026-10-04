"""Production, provider-neutral acquisition layer (public/free sources only)."""
from .base import Activation, FetchResult, SourceAdapter, SourceError, SourceQuality, SourceSpec
from .health import HealthState, ProviderStatus
from .http import SourceHttpClient
from .registry import SourceRegistry, load_source_specs

__all__ = [
    "Activation", "FetchResult", "HealthState", "ProviderStatus", "SourceAdapter", "SourceError",
    "SourceHttpClient", "SourceQuality", "SourceRegistry", "SourceSpec", "load_source_specs",
]
