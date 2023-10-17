from typing import Any, Protocol, TypeAlias
import numpy as np

from utils.LoadModelParams import LoadModelParams


AudioInOut: TypeAlias = np.ndarray[Any, np.dtype[np.int16]]
PitchfInOut: TypeAlias = np.ndarray[Any, np.dtype[np.int16]]
FeatureInOut: TypeAlias = np.ndarray[Any, np.dtype[np.int16]]


class VoiceChangerModel(Protocol):
    # loadModel: Callable[..., dict[str, Any]]
    def loadModel(self, params: LoadModelParams):
        ...

    def get_processing_sampling_rate(self) -> int:
        ...

    def get_info(self) -> dict[str, Any]:
        ...

    def inference(self, data: tuple[Any, ...]) -> Any:
        ...

    def generate_input(self, newData: AudioInOut, inputSize: int, crossfadeSize: int, solaSearchFrame: int) -> tuple[Any, ...]:
        ...

    def update_settings(self, key: str, val: int | float | str) -> bool:
        ...
