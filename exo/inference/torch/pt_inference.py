"""
TorchDynamicShardInferenceEngine
Sharded inference engine using PyTorch based torchtune models
"""
import os
import functools
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import asyncio
import torch
from torchtune.generation import sample as tt_sample
from torchtune.models import llama3

from exo.inference.inference_engine import InferenceEngine
from exo.download.hf.hf_shard_download import HFShardDownloader
from exo.inference.shard import Shard
from exo.inference.tokenizers import _resolve_tokenizer
from exo.helpers import DEBUG
from exo.inference.torch.models.llm_utils import (
  load_model_config,
  load_model_weights_torchtune,
)

# supported models
from exo.inference.torch.models.llama3 import ShardedLlamaModel

TEMP = 0.6
TOP_K = 25

class TorchDynamicShardInferenceEngine(InferenceEngine):
  def __init__(self, shard_downloader: HFShardDownloader):
    self.shard = None
    self.shard_downloader = shard_downloader
    self.request_id = None
    self.executor = ThreadPoolExecutor(max_workers=1)
    self.past_tokens = None
    self.use_llama_tokenizer = os.environ.get("USE_LLAMA_TOKENIZER", False)

    # device settings
    if os.environ.get("TORCH_DEVICE"):
      self.device = torch.device(os.environ["TORCH_DEVICE"])
    elif torch.cuda.is_available():
      self.device = torch.device("cuda")
    elif torch.backends.mps.is_available() and torch.backends.mps.is_built():
      self.device = torch.device("mps")
    else:
      self.device = torch.device("cpu")

  async def encode(self, shard: Shard, prompt: str) -> np.ndarray:
    if DEBUG >= 4:
      print("encode called")
      print(f"shard: {shard}\nprompt: {prompt}")

    await self.ensure_shard(shard)

    tokens = await asyncio.get_event_loop().run_in_executor(
      self.executor,
      functools.partial(
        self.tokenizer.encode,
        prompt
      )
    )

    if isinstance(tokens, list):
      tokens = torch.tensor(tokens).to(device=self.device)

    if DEBUG >= 4:
      print(f"tokens: {tokens}")

    return tokens

  async def decode(self, shard: Shard, tokens: np.ndarray) -> str:
    await self.ensure_shard(shard)
    return await asyncio.get_running_loop().run_in_executor(
      self.executor,
      functools.partial(
        self.tokenizer.decode,
        tokens.tolist()
      )
    )

  async def sample(self, x: np.ndarray, temp=TEMP, top_k=TOP_K) -> np.ndarray:
    logits = x[:, -1]
    def sample_wrapper():
      return tt_sample(
        torch.tensor(logits),
        temperature=temp,
        top_k=top_k
      ).numpy(force=True)

    return await asyncio.get_running_loop().run_in_executor(
      self.executor,
      functools.partial(sample_wrapper)
    )

  async def infer_tensor(
    self,
    request_id: str,
    shard: Shard,
    input_data: np.ndarray,
  ) -> np.ndarray:
    # ensure shard
    if DEBUG >= 4:
      print("infer_tensor called")
      print(f"shard: {shard}")
      print(f"input_data: {input_data}")
      print(f"self.past_tokens: {self.past_tokens}")
    await self.ensure_shard(shard)

    self.request_id = request_id if not self.request_id else self.request_id

    hidden_state = None
    if input_data.shape == (1, 1):
      input_data = torch.tensor(input_data).to(self.device)

      if self.past_tokens is not None:
        self.past_tokens = torch.cat((self.past_tokens, input_data), dim=-1).to(self.device)
      else:
        self.past_tokens = input_data.clone()
    elif input_data.ndim == 3:
      hidden_state = torch.tensor(input_data).to(self.device)

    def infer_wrapper():
      model_hs, model_logits = self.sharded_model.generate(
        tokens=self.past_tokens if hidden_state is not None else None,
        hidden_state=hidden_state
      )

      if model_hs is not None:
        # model_hs = model_hs.detach().cpu()
        return model_hs.numpy(force=True)

      # model_logits = model_logits.detach().cpu()
      token = self.sample(model_logits, TEMP, TOP_K)
      return token.numpy(force=True)

    return await asyncio.get_running_loop().run_in_executor(self.executor, infer_wrapper)

  async def ensure_shard(self, shard: Shard):
    if DEBUG >= 4:
      print("shard ensured\n")
      print(f"shard: {shard}")
      print(f"class shard: {self.shard}")

    if self.shard == shard:
      return
    
    self.shard = shard

    # download model safetensors and shard
    model_path = await self.shard_downloader.ensure_shard(
      shard,
      self.__class__.__name__
    )
    model_config = load_model_config(model_path / "config.json")

    # self.tokenizer = await _resolve_tokenizer(model_path)
    if self.use_llama_tokenizer:
      llama_tokenizer_path = f"{model_path}/original/tokenizer.model"
      self.tokenizer = llama3.llama3_tokenizer(path=llama_tokenizer_path)
    else:
      self.tokenizer = await _resolve_tokenizer(model_path)

    self.sharded_model = await asyncio.get_running_loop().run_in_executor(
      self.executor,
      functools.partial(
        ShardedLlamaModel,
        config=model_config,
        shard=shard,
        device=self.device,
        use_cache=False
      )
    )

    # load sharded weights
    await asyncio.get_running_loop().run_in_executor(
      self.executor,
      functools.partial(
        load_model_weights_torchtune,
        model_path,
        shard,
        self.sharded_model
      )
    )
