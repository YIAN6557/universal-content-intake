"""Provider interface and Stage 2/4 source adapters."""

from .base import ContentProvider, ProviderCapability, ProviderResult
from .video import VideoProvider
from .image import ImageProvider
from .article import ArticleProvider
from .webpage import WebpageProvider

__all__ = ["ContentProvider", "ProviderCapability", "ProviderResult", "VideoProvider", "ImageProvider", "ArticleProvider", "WebpageProvider"]
