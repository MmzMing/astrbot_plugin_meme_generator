"""主进程使用的表情模板数据结构。

模板的实际加载与生成在 worker 子进程里完成，主进程只持有可序列化的元数据，
这样安装/卸载外部库后只需重启 worker，不必重启 AstrBot。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class MemeParams:
    min_images: int
    max_images: int
    min_texts: int
    max_texts: int
    default_texts: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "MemeParams":
        return cls(
            min_images=int(data.get("min_images", 0)),
            max_images=int(data.get("max_images", 0)),
            min_texts=int(data.get("min_texts", 0)),
            max_texts=int(data.get("max_texts", 0)),
            default_texts=[str(item) for item in data.get("default_texts", [])],
        )


@dataclass(slots=True)
class MemeInfo:
    key: str
    keywords: list[str]
    params: MemeParams
    tags: list[str] = field(default_factory=list)
    default_texts: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "MemeInfo":
        params = MemeParams.from_dict(data)
        return cls(
            key=str(data.get("key", "")),
            keywords=[str(item) for item in data.get("keywords", [])],
            params=params,
            tags=[str(item) for item in data.get("tags", [])],
            default_texts=params.default_texts,
        )


@dataclass(slots=True)
class WorkerMeme:
    """worker 中某个模板在主进程的轻量代理。"""

    key: str
    info: MemeInfo

    @classmethod
    def from_dict(cls, data: dict) -> "WorkerMeme":
        info = MemeInfo.from_dict(data)
        return cls(key=info.key, info=info)


@dataclass(slots=True)
class MemeImage:
    """发送给 worker 的图片参数。"""

    name: str
    data: bytes
