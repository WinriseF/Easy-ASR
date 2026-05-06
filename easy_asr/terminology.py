from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_TERMS = [
    {"canonical": "斯坦福", "aliases": ["Stanford"]},
    {"canonical": "机器学习", "aliases": ["machine learning", "ML"]},
    {"canonical": "深度学习", "aliases": ["deep learning"]},
    {"canonical": "特征工程", "aliases": ["feature engineering"]},
    {"canonical": "随机梯度下降", "aliases": ["SGD", "stochastic gradient descent"]},
    {"canonical": "超参数", "aliases": ["hyperparameter"]},
    {"canonical": "神经网络", "aliases": ["neural network"]},
    {"canonical": "卷积神经网络", "aliases": ["CNN", "convolutional neural network"]},
    {"canonical": "循环神经网络", "aliases": ["RNN", "recurrent neural network"]},
    {"canonical": "过拟合", "aliases": ["overfitting"]},
    {"canonical": "欠拟合", "aliases": ["underfitting"]},
    {"canonical": "微调", "aliases": ["fine tuning", "fine-tuning"]},
    {"canonical": "NLP", "aliases": ["自然语言处理"]},
    {"canonical": "Bagging", "aliases": ["bagging"]},
    {"canonical": "Boosting", "aliases": ["boosting"]},
    {"canonical": "Stacking", "aliases": ["stacking"]},
    {"canonical": "SenseVoice", "aliases": ["sense voice"]},
    {"canonical": "FunASR", "aliases": ["fun asr"]},
    {"canonical": "Whisper", "aliases": ["whisper"]},
]


@dataclass
class Term:
    canonical: str
    aliases: list[str] = field(default_factory=list)
    weight: float = 1.0
    case_sensitive: bool = False
    note: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> "Term":
        canonical = str(raw.get("canonical", "")).strip()
        aliases = [str(item).strip() for item in raw.get("aliases", []) if str(item).strip()]
        return cls(
            canonical=canonical,
            aliases=aliases,
            weight=float(raw.get("weight", 1.0)),
            case_sensitive=bool(raw.get("case_sensitive", False)),
            note=str(raw.get("note", "")),
        )

    def to_dict(self) -> dict:
        return {
            "canonical": self.canonical,
            "aliases": self.aliases,
            "weight": self.weight,
            "case_sensitive": self.case_sensitive,
            "note": self.note,
        }


class TerminologyLibrary:
    def __init__(self, terms: list[Term] | None = None):
        self.terms = [term for term in terms or [] if term.canonical]
        self._rules = self._compile_rules()

    @classmethod
    def from_dicts(cls, raws: list[dict]) -> "TerminologyLibrary":
        return cls([Term.from_dict(raw) for raw in raws])

    def to_dicts(self) -> list[dict]:
        return [term.to_dict() for term in self.terms]

    def prompt_text(self, limit: int = 120) -> str:
        values: list[str] = []
        for term in self.terms:
            values.append(term.canonical)
            values.extend(term.aliases)
        deduped = list(dict.fromkeys(value for value in values if value))
        return "，".join(deduped[:limit])

    def apply(self, text: str) -> str:
        result = text
        for pattern, canonical in self._rules:
            result = pattern.sub(canonical, result)
        return result

    def _compile_rules(self) -> list[tuple[re.Pattern[str], str]]:
        rules: list[tuple[re.Pattern[str], str]] = []
        for term in self.terms:
            replacements = [alias for alias in term.aliases if alias and alias != term.canonical]
            replacements.append(term.canonical)
            for source in sorted(set(replacements), key=len, reverse=True):
                if source == term.canonical:
                    continue
                flags = 0 if term.case_sensitive else re.IGNORECASE
                rules.append((re.compile(_term_pattern(source), flags=flags), term.canonical))
        return rules


class TerminologyStore:
    def __init__(self, path: Path):
        self.path = path

    def ensure_default(self) -> None:
        if self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.save(TerminologyLibrary.from_dicts(DEFAULT_TERMS))

    def load(self) -> TerminologyLibrary:
        self.ensure_default()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw_terms = raw.get("terms", [])
        else:
            raw_terms = raw
        return TerminologyLibrary.from_dicts(raw_terms)

    def save(self, library: TerminologyLibrary) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "terms": library.to_dicts(),
            "notes": "aliases 会被确定性替换为 canonical；支持热词/提示词的引擎也会读取这些词。",
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _term_pattern(value: str) -> str:
    escaped = re.escape(value)
    if re.fullmatch(r"[A-Za-z0-9_ .+\-#/]+", value):
        return rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
    return escaped
