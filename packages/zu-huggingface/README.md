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
| Detector | `HfClassifierDetector` | classify an observation → ESCALATE/stop |
| Validator | `HfClassifierValidator` | classify the result → fail/RETRY |

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
  air-gapped / on-prem case. Reaches no network. Every pipeline is built through
  the supply-chain guards. `pip install 'zu-huggingface[local]'` (plus a
  backend such as `torch`).

## The supply chain — safe by default (§8.3)

Pulling a model from the Hub is a supply-chain surface. `supply_chain.py`
enforces, by default:

- **Pin + hash.** A `ModelPin` should carry a full commit-sha `revision`;
  `verify_file_hash` checks a downloaded file's sha256.
- **safetensors, not pickle.** `verify_model_source` rejects `.bin`/`.pt`/`.ckpt`
  checkpoints (which execute on deserialisation) unless explicitly allowed.
- **No remote code.** `safe_pipeline_kwargs` forces `trust_remote_code=False`;
  `assert_no_remote_code` raises if it is relaxed.

The safe configuration is the default — there is nothing to turn *on* to be
safe, only flags a reviewed case may relax.

## Tests

Offline, no network, no model download: the tools and role wrappers are
exercised against a fake `HfClient`, and the supply-chain guards are pure.
`uv run pytest packages/zu-huggingface`.
