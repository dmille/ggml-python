"""ggml-python implemention of the Replit code model

Model is available at:
https://huggingface.co/replit/replit-code-v1-3b

This implementation is based on the example model code and ggml model file format from:
https://github.com/ggerganov/ggml/tree/master/examples/replit
"""
import math
import struct
import ctypes
import argparse
import multiprocessing
from collections import deque
from typing import Deque, List, Sequence, Tuple, Dict

import numpy as np
import numpy.typing as npt

import ggml
from ggml.experimental import GGML_FTYPE, Context, InitParams, Tensor, GGML_TYPE, CGraph


class ReplitLayer:
    def __init__(self, wtype: GGML_TYPE, n_embd: int, ctx: Context):
        self.ln_1_weight = Tensor.new_tensor_1d(GGML_TYPE.F32, n_embd, ctx=ctx)
        self.c_attn_wqkv_weight = Tensor.new_tensor_2d(
            wtype, n_embd, 3 * n_embd, ctx=ctx
        )
        self.c_attn_out_proj_weight = Tensor.new_tensor_2d(
            wtype, n_embd, n_embd, ctx=ctx
        )
        self.ln_2_weight = Tensor.new_tensor_1d(GGML_TYPE.F32, n_embd, ctx=ctx)
        self.c_mlp_mlp_up_weight = Tensor.new_tensor_2d(
            wtype, n_embd, 4 * n_embd, ctx=ctx
        )
        self.c_mlp_mlp_down_weight = Tensor.new_tensor_2d(
            wtype, 4 * n_embd, n_embd, ctx=ctx
        )


class ReplitModel:
    def __init__(
        self,
        d_model: int,
        max_seq_len: int,
        n_heads: int,
        n_layers: int,
        vocab_size: int,
        ftype: int,
        vocab: List[Tuple[int, str, float]],
        ctx: Context,
    ):
        self.d_model = d_model
        self.max_seq_len = max_seq_len
        self.n_heads = n_heads
        self.n_layers = n_layers
        self.vocab_size = vocab_size
        self.ftype = ftype
        self.ctx = ctx
        self.layers: List[ReplitLayer] = []
        self.tensors: Dict[str, Tensor] = {}
        self.vocab = vocab

        n_layer = self.n_layers
        n_embd = self.d_model
        n_ctx = self.max_seq_len
        n_vocab = self.vocab_size
        wtype = GGML_TYPE(ggml.ggml_ftype_to_ggml_type(ctypes.c_int(ftype)))

        n_mem = n_layer * n_ctx
        n_elements = n_embd * n_mem

        self.memory_k = Tensor.new_tensor_1d(GGML_TYPE.F16, n_elements, ctx=ctx)
        self.memory_v = Tensor.new_tensor_1d(GGML_TYPE.F16, n_elements, ctx=ctx)

        self.wte_weight = Tensor.new_tensor_2d(wtype, n_embd, n_vocab, ctx=ctx)
        self.ln_f_weight = Tensor.new_tensor_1d(GGML_TYPE.F32, n_embd, ctx=ctx)
        self.tensors["transformer.wte.weight"] = self.wte_weight
        self.tensors["transformer.norm_f.weight"] = self.ln_f_weight

        for i in range(n_layer):
            layer = ReplitLayer(
                wtype=wtype,
                n_embd=n_embd,
                ctx=ctx,
            )
            self.layers.append(layer)

            self.tensors[f"transformer.blocks.{i}.norm_1.weight"] = layer.ln_1_weight
            self.tensors[
                f"transformer.blocks.{i}.attn.Wqkv.weight"
            ] = layer.c_attn_wqkv_weight
            self.tensors[
                f"transformer.blocks.{i}.attn.out_proj.weight"
            ] = layer.c_attn_out_proj_weight
            self.tensors[f"transformer.blocks.{i}.norm_2.weight"] = layer.ln_2_weight
            self.tensors[
                f"transformer.blocks.{i}.ffn.up_proj.weight"
            ] = layer.c_mlp_mlp_up_weight
            self.tensors[
                f"transformer.blocks.{i}.ffn.down_proj.weight"
            ] = layer.c_mlp_mlp_down_weight

    @staticmethod
    def encode_word(
        word: str, model: Dict[str, Tuple[int, float]]
    ) -> Tuple[List[int], float]:
        best_segmentation_starts = [-1] * (len(word) + 1)
        best_segmentation_starts[0] = 0

        best_segmentation_scores = [-math.inf] * (len(word) + 1)
        best_segmentation_scores[0] = 1.0

        for start_idx in range(len(word)):
            best_score_at_start = best_segmentation_scores[start_idx]
            for end_idx in range(start_idx + 1, len(word) + 1):
                token = word[start_idx:end_idx]
                if token in model and best_score_at_start != -math.inf:
                    token_score = model[token][1]
                    score = token_score + best_score_at_start
                    if (
                        best_segmentation_scores[end_idx] == -math.inf
                        or best_segmentation_scores[end_idx] > score
                    ):
                        best_segmentation_starts[end_idx] = start_idx
                        best_segmentation_scores[end_idx] = score

        if best_segmentation_scores[-1] == -math.inf:
            return [], 0.0

        score = best_segmentation_scores[-1]
        start = best_segmentation_starts[-1]
        end = len(word)
        tokens: Deque[int] = deque()
        while start != 0:
            token_id = model[word[start:end]][0]
            tokens.appendleft(token_id)
            next_start = best_segmentation_starts[start]
            end = start
            start = next_start
        token_id = model[word[start:end]][0]
        tokens.appendleft(token_id)
        return list(tokens), score

    def tokenize(self, text: str) -> List[int]:
        ws_symbol = b"\342\226\201"
        piece_map = {piece: (i, -score) for i, piece, score in self.vocab}
        normalized_text = text.replace(" ", ws_symbol.decode("utf-8"))
        tokenized, _ = ReplitModel.encode_word(normalized_text, piece_map)
        return tokenized

    def detokenize(self, tokens: List[int]) -> str:
        id_to_token = self.vocab
        ws_symbol = b"\342\226\201"
        text = ""
        for token in tokens:
            text += id_to_token[token][1]
        detokenized = text.replace(ws_symbol.decode("utf-8"), " ")
        return detokenized

    def _eval_internal(self, embd_inp: List[int], n_past: int, n_threads: int):
        N = len(embd_inp)

        n_embd = self.d_model
        n_layer = self.n_layers
        n_ctx = self.max_seq_len
        n_head = self.n_heads
        n_vocab = self.vocab_size

        buf_size = 256 * 1024 * 1024
        if not hasattr(self, "buf"):
            self.buf = (ctypes.c_char * buf_size)()

        init_params = InitParams(
            mem_size=buf_size,
            mem_buffer=ctypes.c_void_p(ctypes.addressof(self.buf)),
            no_alloc=False,
        )
        ctx0 = Context(init_params=init_params)
        gf = CGraph(
            cgraph=ggml.ggml_cgraph(
                n_threads=n_threads,
            ),
            ctx=ctx0,
        )

        embd = Tensor.new_tensor_1d(
            GGML_TYPE.I32,
            N,
            ctx=ctx0,
        )
        embd.numpy()[:] = np.array(embd_inp, dtype=np.int32)

        inpL = Tensor.get_rows(self.wte_weight, embd, ctx=ctx0)

        for il in range(n_layer):
            # // a = self.ln_1(x)
            cur = Tensor.norm(inpL, ctx=ctx0)
            cur = Tensor.mul(
                Tensor.repeat(self.layers[il].ln_1_weight, cur, ctx=ctx0),
                cur,
                ctx=ctx0,
            )

            # // self-attention
            # //  b, _, past_key_value = self.attn(a, past_key_value=past_key_value,
            # //  attn_bias=attn_bias, attention_mask=attention_mask,
            # //  is_causal=is_causal)

            # // compute QKV
            cur = Tensor.mul_mat(model.layers[il].c_attn_wqkv_weight, cur, ctx=ctx0)

            Qcur = Tensor.view_2d(
                cur,
                n_embd,
                N,
                cur.tensor.contents.nb[1],
                0 * ctypes.sizeof(ctypes.c_float) * n_embd,
                ctx=ctx0,
            )
            Kcur = Tensor.view_2d(
                cur,
                n_embd,
                N,
                cur.tensor.contents.nb[1],
                1 * ctypes.sizeof(ctypes.c_float) * n_embd,
                ctx=ctx0,
            )
            Vcur = Tensor.view_2d(
                cur,
                n_embd,
                N,
                cur.tensor.contents.nb[1],
                2 * ctypes.sizeof(ctypes.c_float) * n_embd,
                ctx=ctx0,
            )

            # // store key and value to memory
            k = Tensor.view_1d(
                self.memory_k,
                N * n_embd,
                (self.memory_k.element_size() * n_embd) * (il * n_ctx + n_past),
                ctx=ctx0,
            )
            v = Tensor.view_1d(
                self.memory_v,
                N * n_embd,
                (self.memory_v.element_size() * n_embd) * (il * n_ctx + n_past),
                ctx=ctx0,
            )

            gf.build_forward_expand(
                Tensor.cpy(
                    Kcur,
                    k,
                    ctx=ctx0,
                )
            )
            gf.build_forward_expand(
                Tensor.cpy(
                    Vcur,
                    v,
                    ctx=ctx0,
                )
            )

            # // Q = Qcur.contiguous().view(n_embd/n_head, n_head, N).permute(0,
            # // 2, 1, 3) [64, N, 12]
            Q = Tensor.permute(
                Tensor.cpy(
                    Qcur,
                    Tensor.new_tensor_3d(
                        GGML_TYPE.F32, n_embd // n_head, n_head, N, ctx=ctx0
                    ),
                    ctx=ctx0,
                ),
                0,
                2,
                1,
                3,
                ctx=ctx0,
            )

            # // K = Kmem.view(n_embd/n_head, n_head, n_past + N).permute(0, 2, 1,
            # // 3) [64, n_past + N, 12]
            K = Tensor.permute(
                Tensor.reshape_3d(
                    Tensor.view_1d(
                        self.memory_k,
                        (n_past + N) * n_embd,
                        il * n_ctx * self.memory_k.element_size() * n_embd,
                        ctx=ctx0,
                    ),
                    n_embd // n_head,
                    n_head,
                    n_past + N,
                    ctx=ctx0,
                ),
                0,
                2,
                1,
                3,
                ctx=ctx0,
            )

            # // K * Q
            KQ = Tensor.mul_mat(K, Q, ctx=ctx0)

            # // KQ_scaled = KQ / sqrt(n_embd/n_head)
            KQ_scaled = Tensor.scale(
                KQ,
                Tensor.new_f32(
                    1.0 / np.sqrt(float(n_embd) / n_head),
                    ctx=ctx0,
                ),
                ctx=ctx0,
            )

            KQ_scaled_alibi = Tensor.alibi(
                KQ_scaled,
                n_past,
                n_head,
                8.0,
                ctx=ctx0,
            )

            # // KQ_masked = mask_past(KQ_scaled)
            KQ_masked = Tensor.diag_mask_inf(
                KQ_scaled_alibi,
                n_past,
                ctx=ctx0,
            )

            # // KQ = soft_max(KQ_masked)
            KQ_soft_max = Tensor.soft_max(
                KQ_masked,
                ctx=ctx0,
            )

            # // V_trans = Vmem.view(n_embd/n_head, n_head, n_past + N).permute(1,
            # // 2, 0, 3).contiguous() [n_past + N, 64, 12]
            V_trans = Tensor.cpy(
                Tensor.permute(
                    Tensor.reshape_3d(
                        Tensor.view_1d(
                            self.memory_v,
                            (n_past + N) * n_embd,
                            il * n_ctx * self.memory_v.element_size() * n_embd,
                            ctx=ctx0,
                        ),
                        n_embd // n_head,
                        n_head,
                        n_past + N,
                        ctx=ctx0,
                    ),
                    1,
                    2,
                    0,
                    3,
                    ctx=ctx0,
                ),
                Tensor.new_tensor_3d(
                    self.memory_v.ggml_type,
                    n_past + N,
                    n_embd // n_head,
                    n_head,
                    ctx=ctx0,
                ),
            )

            # // KQV = transpose(V) * KQ_soft_max
            KQV = Tensor.mul_mat(V_trans, KQ_soft_max, ctx=ctx0)

            # // KQV_merged = KQV.permute(0, 2, 1, 3)
            KQV_merged = Tensor.permute(
                KQV,
                0,
                2,
                1,
                3,
                ctx=ctx0,
            )

            # // cur = KQV_merged.contiguous().view(n_embd, N)
            cur = Tensor.cpy(
                KQV_merged,
                Tensor.new_tensor_2d(
                    GGML_TYPE.F32,
                    n_embd,
                    N,
                    ctx=ctx0,
                ),
                ctx=ctx0,
            )

            # // projection
            cur = Tensor.mul_mat(
                self.layers[il].c_attn_out_proj_weight,
                cur,
                ctx=ctx0,
            )

            inpL = Tensor.add(
                inpL,
                cur,
                ctx=ctx0,
            )

            # // m = self.ln_2(x)
            cur = Tensor.norm(inpL, ctx=ctx0)
            cur = Tensor.mul(
                Tensor.repeat(self.layers[il].ln_2_weight, cur, ctx=ctx0),
                cur,
                ctx=ctx0,
            )

            # // n = self.mlp(m)
            cur = Tensor.mul_mat(
                self.layers[il].c_mlp_mlp_up_weight,
                cur,
                ctx=ctx0,
            )
            # // GELU activation
            cur = Tensor.gelu(
                cur,
                ctx=ctx0,
            )
            # // projection
            # // cur = proj_w*cur + proj_b
            cur = Tensor.mul_mat(
                self.layers[il].c_mlp_mlp_down_weight,
                cur,
                ctx=ctx0,
            )

            # // x = x + n
            inpL = Tensor.add(
                inpL,
                cur,
                ctx=ctx0,
            )

        # // norm
        inpL = Tensor.norm(inpL, ctx=ctx0)
        # // inpL = ln_f_g*inpL
        inpL = Tensor.mul(
            Tensor.repeat(self.ln_f_weight, inpL, ctx=ctx0),
            inpL,
            ctx=ctx0,
        )

        # // output embedding weight tied to input embedding
        inpL = Tensor.mul_mat(
            self.wte_weight,
            inpL,
            ctx=ctx0,
        )

        gf.build_forward_expand(inpL)
        gf.compute()

        embd_w = inpL.numpy().reshape(n_vocab, -1).copy()

        return embd_w

    @staticmethod
    def eos_token():
        return 1

    @staticmethod
    def init_from_file(
        model_file: str, n_gpu_layers: int = 0, verbose: bool = True
    ) -> "ReplitModel":
        verbose = True

        with open(model_file, "rb") as fin:
            # Magic Number
            (magic,) = struct.unpack("i", (fin.read(struct.calcsize("i"))))
            assert magic == ggml.GGML_FILE_MAGIC.value
            if verbose:
                print("magic number =", hex(magic))
            # Hyperparameters
            d_model, max_seq_len, n_heads, n_layers, vocab_size, ftype = struct.unpack(
                "iiiiii", (fin.read(struct.calcsize("iiiiii")))
            )
            qntvr = ftype // ggml.GGML_QNT_VERSION_FACTOR.value
            if verbose:
                print("d_model      =", d_model)
                print("max_seq_len  =", max_seq_len)
                print("n_heads      =", n_heads)
                print("n_layers     =", n_layers)
                print("vocab_size   =", vocab_size)
                print("ftype        =", ftype)
                print("qntvr        =", qntvr)
            ftype %= ggml.GGML_QNT_VERSION_FACTOR.value
            ftype = GGML_FTYPE(int(ftype))
            # Vocabulary
            vocab: List[Tuple[int, str, float]] = []
            for i in range(vocab_size):
                (s_len,) = struct.unpack("i", (fin.read(struct.calcsize("i"))))
                s = fin.read(s_len).decode("utf-8")
                (score,) = struct.unpack("f", (fin.read(struct.calcsize("f"))))
                vocab.append((i, s, score))
            # Model Weights
            wtype = GGML_TYPE(ggml.ggml_ftype_to_ggml_type(ctypes.c_int(ftype.value)))

            n_embd = d_model
            n_layer = n_layers
            n_ctx = max_seq_len
            n_vocab = vocab_size

            ctx_size = ReplitModel.compute_ctx_size(
                n_embd=n_embd,
                n_layer=n_layer,
                n_ctx=n_ctx,
                n_vocab=n_vocab,
                wtype=wtype,
            )

            if verbose:
                print("ctx size     =", ctx_size // (1024 * 1024), "MB")

            # create context
            mem_buffer = np.empty(ctx_size, dtype=np.uint8)
            init_params = InitParams(
                mem_size=ctx_size,
                mem_buffer=mem_buffer.ctypes.data_as(ctypes.c_void_p),
            )
            ctx = Context(init_params=init_params)

            model = ReplitModel(
                # hyperparameters
                d_model=d_model,
                max_seq_len=max_seq_len,
                n_heads=n_heads,
                n_layers=n_layers,
                vocab_size=vocab_size,
                ftype=ftype.value,
                # vocabulary
                vocab=vocab,
                ctx=ctx,
            )

            n_tensors = 0
            total_size = 0

            while True:
                nbytes = struct.calcsize("iii")
                data = fin.read(nbytes)
                if len(data) != nbytes:
                    break
                n_dims, length, ttype = struct.unpack("iii", data)
                nelements = 1
                ne = [1, 1]
                for i in range(n_dims):
                    (dim,) = struct.unpack("i", (fin.read(struct.calcsize("i"))))
                    ne[i] = dim
                    nelements *= ne[i]
                name = fin.read(length).decode("utf-8")
                if name not in model.tensors:
                    raise ValueError(f"Tensor {name} not found in model")
                tensor = model.tensors[name]
                if tensor.nelements() != nelements:
                    raise ValueError(
                        f"Tensor {name} has {tensor.nelements()} elements, but {nelements} expected"
                    )
                if (
                    tensor.tensor.contents.ne[0] != ne[0]
                    or tensor.tensor.contents.ne[1] != ne[1]
                ):
                    raise ValueError(
                        f"Tensor {name} has {tensor.tensor.contents.ne[0]}x{tensor.tensor.contents.ne[1]} shape, but {ne[0]}x{ne[1]} expected"
                    )
                bpe = ggml.ggml_type_size(ctypes.c_int(GGML_TYPE(ttype).value))
                if (
                    (nelements * bpe) / ggml.ggml_blck_size(tensor.tensor.contents.type)
                ) != ggml.ggml_nbytes(tensor.tensor):
                    raise ValueError(
                        f"Tensor {name} has {ggml.ggml_nbytes(tensor.tensor)} bytes, but {(nelements * bpe) / ggml.ggml_blck_size(tensor.tensor.contents.type)} expected"
                    )
                offset = fin.tell()
                fname = fin.name.encode("utf-8")
                buf = (ctypes.c_char * tensor.nbytes()).from_address(tensor.data)
                fin.readinto(buf)
                # TODO: figure out why offloading norm layers causes segfault
                should_offload_suffix = [
                    # "norm_1.weight",
                    "attn.Wqkv.weight",
                    "attn.out_proj.weight",
                    # "norm_2.weight",
                    "ffn.up_proj.weight",
                    "ffn.down_proj.weight",
                ]
                if name.startswith("transformer.blocks.") and any(
                    name.endswith(suffix) for suffix in should_offload_suffix
                ):
                    layer_number = int(name.split(".")[2])
                    if layer_number >= n_gpu_layers:
                        continue
                    tensor.tensor.contents.backend = ggml.GGML_BACKEND_CUDA.value
                    ggml.ggml_cuda_load_data(
                        fname, tensor.tensor, ctypes.c_size_t(offset)
                    )

                total_size += tensor.nbytes()
                if n_tensors % 8 == 0:
                    print(".", end="", flush=True)
                n_tensors += 1
            print("done")
            print(
                "model size =",
                total_size // (1024 * 1024),
                "MB",
                "num tensors =",
                n_tensors,
            )

        return model

    @staticmethod
    def compute_ctx_size(
        n_embd: int,
        n_layer: int,
        n_ctx: int,
        n_vocab: int,
        wtype: GGML_TYPE,
    ) -> int:
        wtype_sizef = ggml.ggml_type_sizef(ctypes.c_int(wtype.value))
        f32_sizef = ggml.ggml_type_sizef(ctypes.c_int(GGML_TYPE.F32.value))
        f16_sizef = ggml.ggml_type_sizef(ctypes.c_int(GGML_TYPE.F16.value))

        ctx_size = 0
        ctx_size += n_embd * n_vocab * wtype_sizef
        ctx_size += n_embd * f32_sizef

        ctx_size += n_layer * (n_embd * f32_sizef)
        ctx_size += n_layer * (3 * n_embd * n_embd * wtype_sizef)
        ctx_size += n_layer * (n_embd * n_embd * wtype_sizef)
        ctx_size += n_layer * (n_embd * f32_sizef)
        ctx_size += n_layer * (4 * n_embd * n_embd * wtype_sizef)
        ctx_size += n_layer * (n_embd * n_embd * 4 * wtype_sizef)

        ctx_size += n_ctx * n_layer * n_embd * f16_sizef
        ctx_size += n_ctx * n_layer * n_embd * f16_sizef

        ctx_size += (1 + 6 * n_layer) * 512
        ctx_size = int(ctx_size)
        return ctx_size


def sample(
    logits: npt.NDArray[np.float32],
    last_tokens: List[int] = [],
    presence_penalty: float = 0.0,
    frequency_penalty: float = 0.0,
    temperature: float = 1.0,
    top_p: float = 0.0,
) -> int:
    if temperature == 0.0:
        return int(np.argmax(logits))
    logits = frequency_and_presence_penalties(
        logits, last_tokens, frequency_penalty, presence_penalty
    )
    return nucleus_sampling(logits, top_p=top_p, temperature=temperature)


# TODO: this is likely incorrect
def frequency_and_presence_penalties(
    logits: npt.NDArray[np.float32],
    last_tokens: Sequence[int],
    alpha_frequency: float,
    alpha_presence: float,
):
    if len(last_tokens) == 0:
        return logits

    if alpha_frequency == 0.0 and alpha_presence == 0.0:
        return logits

    # Calculate the frequency penalty contribution
    frequency_penalty = alpha_frequency * np.log(np.unique(last_tokens).size + 1)

    # Calculate the presence penalty contribution
    presence_penalty = alpha_presence * np.array(
        [float(token in last_tokens) for token in range(len(logits))]
    )

    # Apply penalties to the logits
    penalized_logits = logits - frequency_penalty - presence_penalty

    return penalized_logits


def nucleus_sampling(
    logits: npt.NDArray[np.float32], top_p: float, temperature: float = 1.0
):
    # Apply temperature to logits
    logits /= temperature

    # Subtract the maximum value for numerical stability
    logits -= np.max(logits)  # type: ignore

    # Calculate probabilities using softmax function with epsilon
    epsilon = 1e-8
    probabilities = np.exp(logits) / (np.sum(np.exp(logits)) + epsilon)  # type: ignore

    # Filter out NaN values from probabilities
    probabilities = np.nan_to_num(probabilities)

    # Sort the probabilities in descending order and get the corresponding indices
    sorted_indices = np.argsort(probabilities)[::-1]

    # Select the indices within the nucleus
    nucleus_indices = sorted_indices[: int(len(sorted_indices) * top_p)]

    # Calculate the updated probabilities within the nucleus
    nucleus_probabilities = probabilities[nucleus_indices]

    # Normalize the probabilities within the nucleus
    nucleus_probabilities /= np.sum(nucleus_probabilities)  # type: ignore

    # Sample from the updated probabilities
    selected_token = np.random.choice(nucleus_indices, p=nucleus_probabilities)

    return selected_token


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--model", type=str, default=None)
    parser.add_argument("-p", "--prompt", type=str, default="def fib(n):")
    parser.add_argument(
        "--n_threads", type=int, default=max(1, multiprocessing.cpu_count() // 2)
    )
    parser.add_argument("--n_gpu_layers", type=int, default=0)
    parser.add_argument("--max_tokens", type=int, default=32)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top_p", type=float, default=1.0)
    parser.add_argument("--presence_penalty", type=float, default=0.0)
    parser.add_argument("--frequency_penalty", type=float, default=0.0)
    args = parser.parse_args()

    model_file = args.model
    n_threads = args.n_threads
    n_gpu_layers = args.n_gpu_layers
    max_tokens = args.max_tokens
    temperature = args.temperature
    top_p = args.top_p
    presence_penalty = args.presence_penalty
    frequency_penalty = args.frequency_penalty

    model = ReplitModel.init_from_file(model_file, n_gpu_layers=n_gpu_layers)

    prompt = args.prompt
    prompt_tokens = model.tokenize(prompt)
    all_tokens: List[int] = prompt_tokens[:]  # type: ignore
    n_past = 0
    tokens: List[int] = prompt_tokens[:]  # type: ignore

    print("number of tokens in prompt =", len(prompt_tokens))
    for i, token_id in enumerate(prompt_tokens):
        print(f"token[{i}] =", token_id)

    print()
    print(prompt, end="", flush=True)
    for i in range(max_tokens):
        # eval
        scores = model._eval_internal(tokens, n_past, n_threads)
        # sample
        logits = scores[:, -1]
        token_id = sample(
            logits,
            last_tokens=all_tokens,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
        )
        if token_id == model.eos_token():
            break
        # update
        all_tokens.append(token_id)
        print(model.detokenize([token_id]), end="", flush=True)
        n_past += len(tokens)
        tokens = [token_id]
    print()
