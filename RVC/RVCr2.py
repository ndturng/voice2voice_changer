"""
VoiceChangerV2
"""
from dataclasses import asdict
from typing import cast

import numpy as np
import resampy
import torch

from mods.log_control import VoiceChangaerLogger
from RVC.deviceManager.DeviceManager import DeviceManager
from RVC.embedder.EmbedderManager import EmbedderManager
from RVC.onnxExporter.export2onnx import export2onnx
from RVC.pipeline.Pipeline import Pipeline
from RVC.pipeline.PipelineGenerator import createPipeline
from RVC.pitchExtractor.PitchExtractorManager import PitchExtractorManager
from RVC.RVCSettings import RVCSettings
from utils.Exceptions import (
    DeviceCannotSupportHalfPrecisionException,
    PipelineCreateException,
    PipelineNotInitializedException,
)
from utils.ModelSlot import RVCModelSlot
from utils.VoiceChangerModel import (
    AudioInOut,
    FeatureInOut,
    PitchfInOut,
    VoiceChangerModel,
)
from utils.VoiceChangerParams import VoiceChangerParams

logger = VoiceChangaerLogger.get_instance().getLogger()


class RVCr2(VoiceChangerModel):
    def __init__(self, params: VoiceChangerParams, slotInfo: RVCModelSlot):
        logger.info("[Voice Changer] [RVCr2] Creating instance ")
        self.deviceManager = DeviceManager.get_instance()
        EmbedderManager.initialize(params)
        PitchExtractorManager.initialize(params)
        self.settings = RVCSettings()
        self.params = params
        # self.pitchExtractor = PitchExtractorManager.getPitchExtractor(self.settings.f0Detector, self.settings.gpu)

        self.pipeline: Pipeline | None = None

        self.audio_buffer: AudioInOut | None = None
        self.pitchf_buffer: PitchfInOut | None = None
        self.feature_buffer: FeatureInOut | None = None
        self.prevVol = 0.0
        self.slotInfo = slotInfo
        # self.initialize()

    def initialize(self):
        logger.info("[Voice Changer][RVCr2] Initializing... ")

        # Generate pipeline
        try:
            self.pipeline = createPipeline(
                self.params,
                self.slotInfo,
                self.settings.gpu,
                self.settings.f0Detector,
            )
            print("pipeline created in RVCr2")
        except PipelineCreateException as e:  # NOQA
            logger.error(
                "[Voice Changer] pipeline create failed. check your model is valid."
            )
            return

        # Other settings
        self.settings.tran = self.slotInfo.defaultTune
        self.settings.indexRatio = self.slotInfo.defaultIndexRatio
        self.settings.protect = self.slotInfo.defaultProtect
        logger.info("[Voice Changer] [RVC] Initializing... done")

    def setSamplingRate(self, inputSampleRate, outputSampleRate):
        self.inputSampleRate = inputSampleRate
        self.outputSampleRate = outputSampleRate
        # self.initialize()

    def update_settings(self, key: str, val: int | float | str):
        logger.info(f"[Voice Changer][RVC]: update_settings {key}:{val}")
        if key in self.settings.intData:
            setattr(self.settings, key, int(val))
            if key == "gpu":
                self.deviceManager.setForceTensor(False)
                self.initialize()
        elif key in self.settings.floatData:
            setattr(self.settings, key, float(val))
        elif key in self.settings.strData:
            setattr(self.settings, key, str(val))
            if key == "f0Detector" and self.pipeline is not None:
                pitchExtractor = PitchExtractorManager.getPitchExtractor(
                    self.settings.f0Detector, self.settings.gpu
                )
                self.pipeline.setPitchExtractor(pitchExtractor)
        else:
            return False
        return True

    def get_info(self):
        data = asdict(self.settings)
        if self.pipeline is not None:
            pipelineInfo = self.pipeline.getPipelineInfo()
            data["pipelineInfo"] = pipelineInfo
        else:
            data["pipelineInfo"] = "None"
        return data

    def get_processing_sampling_rate(self):
        return self.slotInfo.samplingRate

    def generate_input(
        self,
        newData: AudioInOut,
        crossfadeSize: int,
        solaSearchFrame: int,
        extra_frame: int,
    ):
        # It comes in at 16k.
        inputSize = newData.shape[0]
        newData = newData.astype(np.float32) / 32768.0
        newFeatureLength = inputSize // 160  # hopsize:=160

        if self.audio_buffer is not None:
            # Link to past data
            self.audio_buffer = np.concatenate([self.audio_buffer, newData], 0)
            if self.slotInfo.f0:
                self.pitchf_buffer = np.concatenate(
                    [self.pitchf_buffer, np.zeros(newFeatureLength)], 0
                )
            self.feature_buffer = np.concatenate(
                [
                    self.feature_buffer,
                    np.zeros([newFeatureLength, self.slotInfo.embChannels]),
                ],
                0,
            )
        else:
            self.audio_buffer = newData
            if self.slotInfo.f0:
                self.pitchf_buffer = np.zeros(newFeatureLength)
            self.feature_buffer = np.zeros(
                [newFeatureLength, self.slotInfo.embChannels]
            )

        convertSize = inputSize + crossfadeSize + solaSearchFrame + extra_frame

        if (
            convertSize % 160 != 0
        ):  # Compensate for truncation that occurs in the hop size of the model output.
            convertSize = convertSize + (160 - (convertSize % 160))
        outSize = int(
            ((convertSize - extra_frame) / 16000) * self.slotInfo.samplingRate
        )

        # If the buffer is not full, fill it with zero
        if self.audio_buffer.shape[0] < convertSize:
            self.audio_buffer = np.concatenate(
                [np.zeros([convertSize]), self.audio_buffer]
            )
            if self.slotInfo.f0:
                self.pitchf_buffer = np.concatenate(
                    [np.zeros([convertSize // 160]), self.pitchf_buffer]
                )
            self.feature_buffer = np.concatenate(
                [
                    np.zeros([convertSize // 160, self.slotInfo.embChannels]),
                    self.feature_buffer,
                ]
            )

        # Trim unnecessary parts
        convertOffset = -1 * convertSize
        featureOffset = convertOffset // 160
        self.audio_buffer = self.audio_buffer[
            convertOffset:
        ]  # Extract only the part to be converted
        if self.slotInfo.f0:
            self.pitchf_buffer = self.pitchf_buffer[featureOffset:]
        self.feature_buffer = self.feature_buffer[featureOffset:]

        # Cut out only the output part and check the volume.(TODO:mute in stages)
        cropOffset = -1 * (inputSize + crossfadeSize)
        cropEnd = -1 * (crossfadeSize)
        crop = self.audio_buffer[cropOffset:cropEnd]
        vol = np.sqrt(np.square(crop).mean())
        vol = max(vol, self.prevVol * 0.0)
        self.prevVol = vol

        return (
            self.audio_buffer,
            self.pitchf_buffer,
            self.feature_buffer,
            convertSize,
            vol,
            outSize,
        )

    def inference(
        self,
        receivedData: AudioInOut,
        crossfade_frame: int,
        sola_search_frame: int,
    ):
        if self.pipeline is None:
            logger.info("[Voice Changer] Pipeline is not initialized.")
            raise PipelineNotInitializedException()

        # Processing is done at 16K (Pitch, embed, (infer))
        receivedData = cast(
            AudioInOut,
            resampy.resample(
                receivedData,
                self.inputSampleRate,
                16000,
            ),
        )
        crossfade_frame = int((crossfade_frame / self.inputSampleRate) * 16000)
        sola_search_frame = int(
            (sola_search_frame / self.inputSampleRate) * 16000
        )
        extra_frame = int(
            (self.settings.extraConvertSize / self.inputSampleRate) * 16000
        )

        # Generate input data
        data = self.generate_input(
            receivedData, crossfade_frame, sola_search_frame, extra_frame
        )

        audio = data[0]
        pitchf = data[1]
        feature = data[2]
        convertSize = data[3]
        vol = data[4]
        outSize = data[5]

        if vol < self.settings.silentThreshold:
            return np.zeros(convertSize).astype(np.int16) * np.sqrt(vol)

        device = self.pipeline.device

        audio = torch.from_numpy(audio).to(device=device, dtype=torch.float32)
        repeat = 1 if self.settings.rvcQuality else 0
        sid = self.settings.dstId
        f0_up_key = self.settings.tran
        index_rate = self.settings.indexRatio
        protect = self.settings.protect

        if_f0 = 1 if self.slotInfo.f0 else 0
        embOutputLayer = self.slotInfo.embOutputLayer
        useFinalProj = self.slotInfo.useFinalProj

        try:
            (
                audio_out,
                self.pitchf_buffer,
                self.feature_buffer,
            ) = self.pipeline.exec(
                sid,
                audio,
                pitchf,
                feature,
                f0_up_key,
                index_rate,
                if_f0,
                # 0,
                self.settings.extraConvertSize / self.inputSampleRate
                if self.settings.silenceFront
                else 0.0,  # extaraDataSize in seconds. Calculated based on input sampling rate
                embOutputLayer,
                useFinalProj,
                repeat,
                protect,
                outSize,
            )
            # result = audio_out.detach().cpu().numpy() * np.sqrt(vol)
            result = audio_out[-outSize:].detach().cpu().numpy() * np.sqrt(vol)

            result = cast(
                AudioInOut,
                resampy.resample(
                    result,
                    self.slotInfo.samplingRate,
                    self.outputSampleRate,
                ),
            )

            return result
        except DeviceCannotSupportHalfPrecisionException as e:  # NOQA
            logger.warn(
                "[Device Manager] Device cannot support half precision. Fallback to float...."
            )
            self.deviceManager.setForceTensor(True)
            self.initialize()
            # raise e

        return

    def __del__(self):
        del self.pipeline

        # print("---------- REMOVING ---------------")

        # remove_path = os.path.join("RVC")
        # sys.path = [x for x in sys.path if x.endswith(remove_path) is False]

        # for key in list(sys.modules):
        #     val = sys.modules.get(key)
        #     try:
        #         file_path = val.__file__
        #         if file_path.find("RVC" + os.path.sep) >= 0:
        #             # print("remove", key, file_path)
        #             sys.modules.pop(key)
        #     except Exception:  # type:ignore
        #         # print(e)
        #         pass

    def export2onnx(self):
        modelSlot = self.slotInfo

        if modelSlot.isONNX:
            logger.warn("[Voice Changer] export2onnx, No pyTorch filepath.")
            return {"status": "ng", "path": ""}

        if self.pipeline is not None:
            del self.pipeline
            self.pipeline = None

        torch.cuda.empty_cache()
        self.initialize()

        output_file_simple = export2onnx(self.settings.gpu, modelSlot)

        return {
            "status": "ok",
            "path": f"/tmp/{output_file_simple}",
            "filename": output_file_simple,
        }

    def get_model_current(self):
        return [
            {
                "key": "defaultTune",
                "val": self.settings.tran,
            },
            {
                "key": "defaultIndexRatio",
                "val": self.settings.indexRatio,
            },
            {
                "key": "defaultProtect",
                "val": self.settings.protect,
            },
        ]
