# zu-huggingface

HuggingFace models behind Zu's typed ports. HuggingFace is not a model — it is
the largest hub of open models across every modality — so "supporting it" means
three different things, and this package draws the line cleanly
(Engineering Design §8.3–8.5).

## Chat / vision-language models as the policy — *no code here*

A chat or vision-language model that is the **brain** speaks the OpenAI chat API
on all three HuggingFace serving surfaces (the router's `/v1`, a dedicated
Endpoint's `/v1`, or a local vLLM server). So a HuggingFace model as the policy
is the existing `openai-compatible` provider pointed at a HuggingFace base URL —
the OpenRouter story exactly, no new adapter:

```yaml
# agent.yaml — a HuggingFace multimodal model as the policy
model:    meta-llama/Llama-Vision-...        # any chat / VLM id on the Hub
provider: openai-compatible
options:
  base_url: https://router.huggingface.co/v1  # or an Endpoint, or local vLLM
  api_key_env: HF_TOKEN
```

The **three serving surfaces are one adapter + config** — only the `base_url`
changes (the path is always `<base_url>/chat/completions`):

| Surface | `base_url` |
|---------|-----------|
| Inference Providers router | `https://router.huggingface.co/v1` |
| Dedicated Inference Endpoint | `https://<id>.<region>.aws.endpoints.huggingface.cloud/v1` |
| Local vLLM | `http://localhost:8000/v1` |

A **VLM policy** (an image in the chat request) rides the *same* adapter+config:
a multimodal `content` list (`{type:"text"}` + `{type:"image_url", image_url:{url:
"data:<mime>;base64,…"}}`) passes straight through to the wire. This is proven
offline by `zu-providers/tests/test_hf_router_policy.py` (an `httpx.MockTransport`
asserting the request path, the `Bearer` from `HF_TOKEN`, the body, and that the
response parses identically across all three base URLs — no live call).

## Task models as tools, detectors, validators — this package

Most HuggingFace models are **not** chat models (OCR, ASR, detection,
embeddings, classification, …), so they enter through the non-policy ports by
their **role** (the port is the role, assigned per agent — §4.5):

| Role | Class | Task |
|------|-------|------|
| Tool | `Transcribe` (`hf_transcribe`) | speech → text (ASR) |
| Tool | `ImageToText` (`hf_image_to_text`) | image → text (OCR / caption) |
| Tool | `DetectObjects` (`hf_detect`) | image → labelled boxes |
| Tool | `Embed` (`hf_embed`) | text → vector (retrieval / grounding) |
| Tool | `Classify` (`hf_classify`) | text → labels |
| Tool | `ZeroShotClassify` (`hf_zero_shot`) | text + labels → scores |
| Tool | `Summarize` (`hf_summarize`) | text → text |
| Tool | `Translate` (`hf_translate`) | text → text |
| Tool | `SegmentImage` (`hf_segment`) | image → labelled masks |
| Tool | `EstimateDepth` (`hf_depth`) | image → depth map (base64 PNG) |
| Tool | `AskDocument` (`hf_doc_qa`) | document image + question → answer |
| Tool | `AskImage` (`hf_vqa`) | image + question → answer (VQA) |
| Tool | `Speak` (`hf_speak`) | text → audio (base64 WAV) |
| Tool | `ClassifyAudio` (`hf_classify_audio`) | audio → labels (same shape as `Classify`) |
| Tool | `VlmDescribe` (`hf_vlm`) | **image + text prompt → text** (VLM-as-tool) |
| Tool | `AskTable` (`hf_table_qa`) | table + question → answer |
| Tool | `ClassifyTable` (`hf_tabular_classify`) | rows → label per row (hosted-only) |
| Tool | `PredictTable` (`hf_tabular_regress`) | rows → number per row (hosted-only) |
| Detector | `HfClassifierDetector` | classify an observation → ESCALATE/stop |
| Validator | `HfClassifierValidator` | classify the result → fail/RETRY |

**VLM-as-tool.** `VlmDescribe` exposes a vision-language model's vision as a
*verb* (not the policy): a **text** policy can call `hf_vlm(image, prompt)` to
get a description/answer about a picture and then reason over it. It rides the
client's `image_text_to_text` path — a multimodal chat call hosted (a `text` +
`image_url` data-URL message), an `image-text-to-text` pipeline local — over the
one `HfClient` seam, exactly like every other tool.

**Tabular** (`ClassifyTable`/`PredictTable`) is **hosted-only**: tabular models
are sklearn/tabular-backed on the Hub and served via the Inference API, so the
local `PipelineBackend` raises a clear hosted-only error rather than fetch a
model (it therefore cannot bypass the supply-chain guard).

Each is **parameterised by a model id** (and the role wrappers by the labels
that matter), so they are wired *by reference in config* per agent rather than
as zero-config entry points:

```yaml
tools:
  - ref: zu_huggingface.tools:Transcribe
    args: { model: openai/whisper-large-v3 }
  - ref: zu_huggingface.tools:Embed
    args: { model: BAAI/bge-large-en-v1.5 }
detectors:
  - ref: zu_huggingface.roles:HfClassifierDetector
    args: { model: facebook/bart-large-mnli, candidate_labels: ["safe","unsafe"], escalate_on: ["unsafe"] }
```

The typed multimodal `Content` (`Text`/`Image`/`Audio`) from `zu_core.content`
is the currency in and out — which is what lets a non-chat model slot into the
loop as cleanly as a chat one.

### Hosted vs local — one seam

Every tool depends only on the `HfClient` seam, so the same tool works:

- **Hosted** — `InferenceClientBackend` wraps `huggingface_hub.InferenceClient`
  (the serverless router or a dedicated Endpoint). Egresses to
  `router.huggingface.co`; `HF_TOKEN` is read from the environment inside the
  backend. `pip install 'zu-huggingface[hosted]'`.
- **Local** — `PipelineBackend` wraps `transformers.pipeline` for the
  air-gapped / on-prem case. Reaches no network (fails closed on a cache miss;
  populate the cache out of band): the file set is resolved from the local cache
  via `snapshot_download(..., local_files_only=True)`, every pipeline is built
  with `local_files_only=True` / `use_safetensors=True`, and the process runs
  with `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`. Every pipeline is built
  through the supply-chain guards. `pip install 'zu-huggingface[local]'` (plus a
  backend such as `torch`).

## The supply chain — safe by default (§8.3)

Pulling a model from the Hub is a supply-chain surface. `supply_chain.py`
enforces, by default:

- **Pin + hash.** A `ModelPin` carries a full commit-sha `revision` and optional
  `expected_hashes`; on the local path the `PipelineBackend` resolves the cached
  snapshot offline and `verify_file_hash` checks each entry's sha256 against the
  file on disk *before* the pipeline is constructed.
- **safetensors, not pickle.** `verify_model_source` rejects `.bin`/`.pt`/`.ckpt`
  checkpoints (which execute on deserialisation) unless explicitly allowed — and
  on the local path this runs against the **real** cached file set before load
  (with `use_safetensors=True` as loader-level defence-in-depth).
- **No remote code.** `safe_pipeline_kwargs` forces `trust_remote_code=False`;
  `assert_no_remote_code` raises if it is relaxed.

The safe configuration is the default — there is nothing to turn *on* to be
safe, only flags a reviewed case may relax.

## Tests

Offline, no network, no model download: the tools and role wrappers are
exercised against a fake `HfClient`, and the supply-chain guards are pure.
`uv run pytest packages/zu-huggingface`.
