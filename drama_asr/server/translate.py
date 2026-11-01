"""Translation backends.

The real backend runs NLLB-200 locally via Hugging Face transformers so
translation stays fully offline and uncensored. Heavy libraries are imported
lazily.
"""

from __future__ import annotations

from typing import Protocol


class Translator(Protocol):
    def translate_batch(self, texts: list[str]) -> list[str]:
        ...


class NllbTranslator:
    """Local NLLB-200 ja->zh translator."""

    def __init__(
        self,
        model_name: str,
        source_lang: str = "jpn_Jpan",
        target_lang: str = "zho_Hans",
        device: str = "cuda",
        batch_size: int = 16,
        max_length: int = 512,
    ) -> None:
        import torch  # lazy import
        from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

        self._torch = torch
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.batch_size = batch_size
        self.max_length = max_length

        self._tokenizer = AutoTokenizer.from_pretrained(model_name, src_lang=source_lang)
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
        use_cuda = device.startswith("cuda") and torch.cuda.is_available()
        if use_cuda:
            model = model.to("cuda").half()
        self._device = "cuda" if use_cuda else "cpu"
        self._model = model.eval()
        self._bos_token_id = self._tokenizer.convert_tokens_to_ids(target_lang)

    def translate_batch(self, texts: list[str]) -> list[str]:
        if not texts:
            return []
        out: list[str] = []
        torch = self._torch
        for i in range(0, len(texts), self.batch_size):
            chunk = texts[i : i + self.batch_size]
            encoded = self._tokenizer(
                chunk,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=self.max_length,
            ).to(self._device)
            with torch.inference_mode():
                generated = self._model.generate(
                    **encoded,
                    forced_bos_token_id=self._bos_token_id,
                    max_length=self.max_length,
                    num_beams=4,
                )
            out.extend(
                self._tokenizer.batch_decode(generated, skip_special_tokens=True)
            )
        return out


class MockTranslator:
    """Deterministic stub: prefixes text so the pipeline can be verified."""

    def translate_batch(self, texts: list[str]) -> list[str]:
        return [f"[译] {t}" for t in texts]


class NoopTranslator:
    """Used when translation is disabled: returns no translations."""

    def translate_batch(self, texts: list[str]) -> list[str]:
        return ["" for _ in texts]


def build_translator(settings) -> Translator:
    if not settings.enable_translation:
        return NoopTranslator()
    if settings.mock:
        return MockTranslator()
    return NllbTranslator(
        model_name=settings.nllb_model,
        source_lang=settings.source_lang,
        target_lang=settings.target_lang,
        device=settings.device,
        batch_size=settings.translate_batch_size,
    )
