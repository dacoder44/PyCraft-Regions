"""Conservative release-only allowlist. Snapshots are intentionally excluded."""

from dataclasses import dataclass

from .exceptions import CoordinateError, UnsupportedFormatError

RELEASES = {
    1519: "1.13",
    1628: "1.13.1",
    1631: "1.13.2",
    1952: "1.14",
    1957: "1.14.1",
    1963: "1.14.2",
    1968: "1.14.3",
    1976: "1.14.4",
    2225: "1.15",
    2227: "1.15.1",
    2230: "1.15.2",
    2566: "1.16",
    2567: "1.16.1",
    2578: "1.16.2",
    2580: "1.16.3",
    2584: "1.16.4",
    2586: "1.16.5",
    2724: "1.17",
    2730: "1.17.1",
    2860: "1.18",
    2865: "1.18.1",
    2975: "1.18.2",
    3105: "1.19",
    3117: "1.19.1",
    3120: "1.19.2",
    3218: "1.19.3",
    3337: "1.19.4",
    3463: "1.20",
    3465: "1.20.1",
    3578: "1.20.2",
    3698: "1.20.3",
    3700: "1.20.4",
}


@dataclass(frozen=True)
class Version:
    data_version: int

    def __post_init__(self) -> None:
        if self.data_version not in RELEASES:
            raise UnsupportedFormatError(
                f"Unsupported DataVersion {self.data_version}; only release versions "
                "1.13–1.20.4 are enabled (no snapshots, legacy or newer formats)"
            )

    @property
    def modern(self) -> bool:
        return self.data_version >= 2860

    @property
    def padded(self) -> bool:
        return self.data_version >= 2566

    @property
    def name(self) -> str:
        return RELEASES[self.data_version]

    def bounds(self, dimension: str) -> tuple[int, int]:
        if dimension == "minecraft:overworld":
            return (-64, 320) if self.modern else (0, 256)
        if dimension in ("minecraft:the_nether", "minecraft:the_end"):
            return 0, 256
        raise CoordinateError(
            f"Unsupported dimension {dimension!r}; only vanilla dimension heights are supported"
        )
