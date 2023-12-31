import os
import traceback
import faiss
from utils.Exceptions import PipelineCreateException
from utils.ModelSlot import RVCModelSlot

from RVC.deviceManager.DeviceManager import DeviceManager
from RVC.embedder.EmbedderManager import EmbedderManager
from RVC.inferencer.InferencerManager import InferencerManager
from RVC.pipeline.Pipeline import Pipeline
from RVC.pitchExtractor.PitchExtractorManager import PitchExtractorManager
from utils.VoiceChangerParams import VoiceChangerParams


def createPipeline(params: VoiceChangerParams, modelSlot: RVCModelSlot, gpu: int, f0Detector: str):
    dev = DeviceManager.get_instance().getDevice(gpu)
    half = DeviceManager.get_instance().halfPrecisionAvailable(gpu)

    # Inferencer generation
    try:
        modelPath = os.path.join(params.model_dir, str(modelSlot.slotIndex), os.path.basename(modelSlot.modelFile))
        inferencer = InferencerManager.getInferencer(modelSlot.modelType, modelPath, gpu, modelSlot.version)
    except Exception as e:
        print("[Voice Changer] exception! loading inferencer", e)
        traceback.print_exc()
        raise PipelineCreateException("[Voice Changer] exception! loading inferencer")

    # Embedder generation
    try:
        embedder = EmbedderManager.getEmbedder(
            modelSlot.embedder,
            # emmbedderFilename,
            half,
            dev,
        )
    except Exception as e:
        print("[Voice Changer] exception! loading embedder", e, dev)
        traceback.print_exc()
        raise PipelineCreateException("[Voice Changer] exception! loading embedder")

    # pitchExtractor
    pitchExtractor = PitchExtractorManager.getPitchExtractor(f0Detector, gpu)

    # index, feature
    indexPath = os.path.join(params.model_dir, str(modelSlot.slotIndex), os.path.basename(modelSlot.indexFile))
    index = _loadIndex(indexPath)

    pipeline = Pipeline(
        embedder,
        inferencer,
        pitchExtractor,
        index,
        modelSlot.samplingRate,
        dev,
        half,
    )

    return pipeline


def _loadIndex(indexPath: str):
    # Loading index
    print("[Voice Changer] Loading index...")
    # None if there is no file even if there is a file specified
    if os.path.exists(indexPath) is not True or os.path.isfile(indexPath) is not True:
        print("[Voice Changer] Index file is not found")
        return None

    try:
        print("Try loading...", indexPath)
        index = faiss.read_index(indexPath)
    except: # NOQA
        print("[Voice Changer] load index failed. Use no index.")
        traceback.print_exc()
        return None

    return index
