"""Deterministic, run-local persona repair for the RN Community AB study.

The legacy ``outputs/sys_100.db`` is a source artifact, not an RN runtime
database.  Its structured cohort fields are authoritative, but 60 stored
prompt depth statements disagree with ``news_depth`` and one prompt lacks line
breaks.  This module copies that database without changing it, rerenders only
``persona_prompt`` from structured fields, and seals the result with manifests
that a runner can verify before any model call.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import stat
import tempfile
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from twinmarket_kr.rn_ab.memory import canonical_json, scientific_sha256


class PersonaSnapshotError(RuntimeError):
    """A source cohort or a sealed persona snapshot violates the RN contract."""


SNAPSHOT_VERSION = "rn-persona-snapshot-v1"
RENDERER_ID = "render_persona_v1_nfc_lf_one-trailing-lf_fixed-sections"
SNAPSHOT_DB_FILENAME = "persona_snapshot.sqlite"
SNAPSHOT_MANIFEST_FILENAME = "persona_snapshot_manifest.json"
DEPTH_MANIFEST_FILENAME = "persona_depth_manifest.json"
REPAIR_MANIFEST_FILENAME = "persona_repair_manifest.json"

AGENT_COLUMNS = (
    "agent_id",
    "source_user_id",
    "user_type",
    "gender",
    "age",
    "age_group",
    "location",
    "bh_disposition_effect_category",
    "bh_lottery_preference_category",
    "bh_total_return_category",
    "bh_underdiversification_category",
    "strategy",
    "trad_pro",
    "fol_ind",
    "ini_cash",
    "news_depth",
    "segment_key",
    "match_score",
    "persona_prompt",
)
_STRUCTURED_COLUMNS = tuple(column for column in AGENT_COLUMNS if column != "persona_prompt")
_SHA256_HEX_LENGTH = 64

_GENDER_LABELS = {"male": "남성", "female": "여성"}
_USER_TYPE_LABELS = {
    "ordinary": "일반 개인투자자",
    "small_influencer": "팔로워가 적은 투자 인플루언서",
    "big_influencer": "영향력이 큰 투자 인플루언서",
}
_DISPOSITION_LABELS = {
    "high": "수익이 나면 빠르게 매도하고 손실 시 추가 매수하는 경향이 강합니다",
    "medium": "수익과 손실 상황 모두에서 비교적 균형 잡힌 판단을 하는 편입니다",
    "low": "수익은 오래 보유하고 손실 시 이성적으로 손절하는 편입니다",
}
_LOTTERY_LABELS = {
    "high": "고위험 고수익 기회를 적극적으로 선호합니다",
    "medium": "적정 수준의 위험을 수용합니다",
    "low": "안정적이고 검증된 자산을 선호합니다",
}
_RETURN_LABELS = {
    "high": "과거 투자 성과가 좋은 편입니다",
    "medium": "과거 투자 성과가 평균적인 편입니다",
    "low": "과거 투자 성과가 낮은 편입니다",
}
_UNDERDIVERSIFICATION_LABELS = {
    "low": "비교적 잘 분산된 포트폴리오를 유지하는 편입니다",
    "medium": "특정 종목에 다소 집중하는 성향이 있습니다",
    "high": "특정 종목에 매우 집중하는 성향이 있습니다",
}
_STRATEGY_LABELS = {
    "technical": "기술적 지표, 추세, 거래량, 이동평균, 돌파 신호를 기반으로 판단합니다",
    "value": "PE/PB 등 가치평가 지표, 내재가치, 성장성, 저평가 여부를 기반으로 판단합니다",
}
_DEPTH_LABELS = {
    0: "당일 헤드라인만 확인하는 헤드라인 스캔형입니다.",
    1: "당일 헤드라인과 허용된 요약을 확인하는 요약 전독형입니다.",
    2: "당일 헤드라인과 허용된 요약을 확인하고, 정책상 허용된 최근 뉴스 탐색을 할 수 있는 심층 탐색형입니다.",
}


def _reverse(mapping: Mapping[Any, str]) -> dict[str, Any]:
    return {value: key for key, value in mapping.items()}


_GENDER_CODES = _reverse(_GENDER_LABELS)
_USER_TYPE_CODES = _reverse(_USER_TYPE_LABELS)
_DISPOSITION_CODES = _reverse(_DISPOSITION_LABELS)
_LOTTERY_CODES = _reverse(_LOTTERY_LABELS)
_RETURN_CODES = _reverse(_RETURN_LABELS)
_UNDERDIVERSIFICATION_CODES = _reverse(_UNDERDIVERSIFICATION_LABELS)
_STRATEGY_CODES = _reverse(_STRATEGY_LABELS)
_DEPTH_CODES = _reverse(_DEPTH_LABELS)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PersonaSnapshotError(f"{label} must be a non-empty string")
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _required_int(value: Any, *, label: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PersonaSnapshotError(f"{label} must be an integer >= {minimum}")
    return int(value)


def _canonical_interest_industries(value: Any) -> str:
    text = _canonical_text(value, label="fol_ind")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PersonaSnapshotError("fol_ind must be a JSON array") from exc
    if not isinstance(parsed, list) or not parsed or any(not isinstance(item, str) or not item.strip() for item in parsed):
        raise PersonaSnapshotError("fol_ind must be a non-empty array of non-empty strings")
    return json.dumps(
        [unicodedata.normalize("NFC", item).strip() for item in parsed],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _normalized_agent_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if set(row) != set(AGENT_COLUMNS):
        raise PersonaSnapshotError(
            "agents row schema differs from the frozen legacy cohort; "
            f"missing={sorted(set(AGENT_COLUMNS) - set(row))} "
            f"extra={sorted(set(row) - set(AGENT_COLUMNS))}"
        )
    result: dict[str, Any] = {}
    for name in _STRUCTURED_COLUMNS:
        result[name] = row[name]
    result["agent_id"] = _canonical_text(result["agent_id"], label="agent_id")
    result["source_user_id"] = _canonical_text(result["source_user_id"], label="source_user_id")
    for name, labels in (
        ("user_type", _USER_TYPE_LABELS),
        ("gender", _GENDER_LABELS),
        ("bh_disposition_effect_category", _DISPOSITION_LABELS),
        ("bh_lottery_preference_category", _LOTTERY_LABELS),
        ("bh_total_return_category", _RETURN_LABELS),
        ("bh_underdiversification_category", _UNDERDIVERSIFICATION_LABELS),
        ("strategy", _STRATEGY_LABELS),
    ):
        value = _canonical_text(result[name], label=name)
        if value not in labels:
            raise PersonaSnapshotError(f"{name} has an unsupported value for RN renderer: {value!r}")
        result[name] = value
    for name in ("age_group", "location", "segment_key"):
        result[name] = _canonical_text(result[name], label=name)
    result["age"] = _required_int(result["age"], label="age", minimum=1)
    result["trad_pro"] = _required_int(result["trad_pro"], label="trad_pro", minimum=0)
    result["ini_cash"] = _required_int(result["ini_cash"], label="ini_cash", minimum=1)
    result["news_depth"] = _required_int(result["news_depth"], label="news_depth", minimum=0)
    if result["news_depth"] not in _DEPTH_LABELS:
        raise PersonaSnapshotError("news_depth must be one of 0, 1, 2")
    result["match_score"] = _required_int(result["match_score"], label="match_score", minimum=0)
    result["fol_ind"] = _canonical_interest_industries(result["fol_ind"])
    result["persona_prompt"] = _canonical_text(row["persona_prompt"], label="persona_prompt")
    return result


def render_persona_v1(row: Mapping[str, Any]) -> str:
    """Render the only RN persona prompt projection from structured fields.

    It deliberately does not read the old ``persona_prompt`` value.  That
    keeps ``news_depth`` a single source of truth and makes a malformed legacy
    blob (such as A001) incapable of contaminating the paper prompt.
    """
    agent = _normalized_agent_row(row)
    lines = (
        "당신은 한국의 삼성전자 개인투자자입니다.",
        "[기본 정보]",
        f"성별: {_GENDER_LABELS[agent['gender']]}",
        f"나이: {agent['age']}세",
        f"연령대: {agent['age_group']}",
        f"거주 지역: {agent['location']}",
        f"투자자 유형: {_USER_TYPE_LABELS[agent['user_type']]}",
        "[투자 성향]",
        f"처분효과: {_DISPOSITION_LABELS[agent['bh_disposition_effect_category']]}",
        f"위험자산 선호: {_LOTTERY_LABELS[agent['bh_lottery_preference_category']]}",
        f"성과 경험: {_RETURN_LABELS[agent['bh_total_return_category']]}",
        f"분산투자 성향: {_UNDERDIVERSIFICATION_LABELS[agent['bh_underdiversification_category']]}",
        f"투자 전략: {_STRATEGY_LABELS[agent['strategy']]}",
        f"거래 경험 지표: {agent['trad_pro']}",
        f"관심 산업: {agent['fol_ind']}",
        "[정보 접근]",
        f"뉴스 접근 단계: D{agent['news_depth']}",
        f"뉴스 접근 방식: {_DEPTH_LABELS[agent['news_depth']]}",
        "[거래 범위]",
        "거래 종목: 삼성전자(005930)",
        f"초기 자산: 현금 {agent['ini_cash']:,}원, 주식 0주",
    )
    return unicodedata.normalize("NFC", "\n".join(lines)).replace("\r\n", "\n").replace("\r", "\n") + "\n"


def parse_persona_v1(value: str) -> dict[str, Any]:
    """Parse the canonical renderer output and reject any byte-level drift."""
    if not isinstance(value, str):
        raise PersonaSnapshotError("persona prompt must be a string")
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    if normalized != value or not normalized.endswith("\n") or normalized.endswith("\n\n"):
        raise PersonaSnapshotError("persona prompt must be NFC, LF-only, and end in exactly one LF")
    lines = normalized[:-1].split("\n")
    if len(lines) != 21:
        raise PersonaSnapshotError("persona prompt does not have the fixed v1 section count")
    expected_literals = {
        0: "당신은 한국의 삼성전자 개인투자자입니다.",
        1: "[기본 정보]",
        7: "[투자 성향]",
        15: "[정보 접근]",
        18: "[거래 범위]",
        19: "거래 종목: 삼성전자(005930)",
    }
    if any(lines[index] != literal for index, literal in expected_literals.items()):
        raise PersonaSnapshotError("persona prompt does not use the fixed v1 section labels")

    def suffix(index: int, prefix: str) -> str:
        if not lines[index].startswith(prefix):
            raise PersonaSnapshotError(f"persona prompt field is malformed: {prefix}")
        return lines[index][len(prefix) :]

    gender_label = suffix(2, "성별: ")
    user_type_label = suffix(6, "투자자 유형: ")
    disposition_label = suffix(8, "처분효과: ")
    lottery_label = suffix(9, "위험자산 선호: ")
    return_label = suffix(10, "성과 경험: ")
    underdiv_label = suffix(11, "분산투자 성향: ")
    strategy_label = suffix(12, "투자 전략: ")
    depth_label = suffix(16, "뉴스 접근 단계: ")
    depth_description = suffix(17, "뉴스 접근 방식: ")
    age_text = suffix(3, "나이: ")
    if not age_text.endswith("세") or not age_text[:-1].isdigit() or int(age_text[:-1]) < 1:
        raise PersonaSnapshotError("persona age line is malformed")
    experience_text = suffix(13, "거래 경험 지표: ")
    if not experience_text.isdigit():
        raise PersonaSnapshotError("persona trading-experience line is malformed")
    if not depth_label.startswith("D") or not depth_label[1:].isdigit():
        raise PersonaSnapshotError("persona depth line is malformed")
    depth = int(depth_label[1:])
    if depth not in _DEPTH_LABELS or _DEPTH_LABELS[depth] != depth_description:
        raise PersonaSnapshotError("persona depth description differs from the canonical depth policy")
    cash_text = suffix(20, "초기 자산: 현금 ")
    if not cash_text.endswith("원, 주식 0주"):
        raise PersonaSnapshotError("persona initial-cash line is malformed")
    raw_cash = cash_text[: -len("원, 주식 0주")].replace(",", "")
    if not raw_cash.isdigit() or int(raw_cash) < 1:
        raise PersonaSnapshotError("persona initial cash must be positive")
    industries = _canonical_interest_industries(suffix(14, "관심 산업: "))
    parsed = {
        "gender": _lookup(_GENDER_CODES, gender_label, "gender"),
        "age": int(age_text[:-1]),
        "age_group": _canonical_text(suffix(4, "연령대: "), label="age_group"),
        "location": _canonical_text(suffix(5, "거주 지역: "), label="location"),
        "user_type": _lookup(_USER_TYPE_CODES, user_type_label, "user_type"),
        "bh_disposition_effect_category": _lookup(_DISPOSITION_CODES, disposition_label, "disposition"),
        "bh_lottery_preference_category": _lookup(_LOTTERY_CODES, lottery_label, "lottery"),
        "bh_total_return_category": _lookup(_RETURN_CODES, return_label, "return"),
        "bh_underdiversification_category": _lookup(
            _UNDERDIVERSIFICATION_CODES, underdiv_label, "underdiversification"
        ),
        "strategy": _lookup(_STRATEGY_CODES, strategy_label, "strategy"),
        "trad_pro": int(experience_text),
        "fol_ind": industries,
        "ini_cash": int(raw_cash),
        "news_depth": depth,
    }
    return parsed


def _lookup(mapping: Mapping[str, Any], value: str, label: str) -> Any:
    try:
        return mapping[value]
    except KeyError as exc:
        raise PersonaSnapshotError(f"persona {label} has an unknown canonical label") from exc


def persona_renderer_sha256() -> str:
    """Hash source plus stable renderer vocabulary, not an environment path."""
    payload = {
        "renderer_id": RENDERER_ID,
        "render_source": inspect.getsource(render_persona_v1),
        "parse_source": inspect.getsource(parse_persona_v1),
        "vocabulary": {
            "gender": _GENDER_LABELS,
            "user_type": _USER_TYPE_LABELS,
            "disposition": _DISPOSITION_LABELS,
            "lottery": _LOTTERY_LABELS,
            "return": _RETURN_LABELS,
            "underdiversification": _UNDERDIVERSIFICATION_LABELS,
            "strategy": _STRATEGY_LABELS,
            "depth": _DEPTH_LABELS,
        },
    }
    return scientific_sha256(payload)


@dataclass(frozen=True)
class FrozenPersona:
    agent_id: str
    news_depth: int
    initial_cash: int
    persona_prompt: str
    persona_sha256: str
    structured_row_sha256: str


@dataclass(frozen=True)
class PersonaSnapshotArtifacts:
    snapshot_dir: Path
    snapshot_db_path: Path
    snapshot_manifest_path: Path
    depth_manifest_path: Path
    repair_manifest_path: Path
    source_db_sha256: str
    snapshot_db_sha256: str
    prompt_map_sha256: str


@dataclass(frozen=True)
class SealedPersonaSnapshot:
    """Read-only runtime view; never falls back to the legacy global DB."""

    snapshot_dir: Path
    snapshot_db_path: Path
    manifest_sha256: str
    source_db_sha256: str
    snapshot_db_sha256: str
    prompt_map_sha256: str
    depth_manifest_sha256: str
    repair_manifest_sha256: str
    personas: Mapping[str, FrozenPersona]

    @classmethod
    def load(cls, snapshot_dir: Path | str) -> "SealedPersonaSnapshot":
        root = Path(snapshot_dir)
        manifest_path = root / SNAPSHOT_MANIFEST_FILENAME
        db_path = root / SNAPSHOT_DB_FILENAME
        manifest = _read_canonical_manifest(manifest_path, label="RN persona snapshot manifest")
        required = {
            "artifact_type",
            "version",
            "source_db_sha256",
            "snapshot_db_sha256",
            "renderer",
            "agent_count",
            "depth_counts",
            "ordered_agent_prompt_map_sha256",
            "agents",
        }
        if not isinstance(manifest, Mapping) or set(manifest) != required:
            raise PersonaSnapshotError("RN persona snapshot manifest has an invalid schema")
        if manifest.get("artifact_type") != "rn_persona_snapshot" or manifest.get("version") != SNAPSHOT_VERSION:
            raise PersonaSnapshotError("RN persona snapshot manifest has an unexpected type/version")
        if not db_path.exists() or _sha256_file(db_path) != _sha256_text(
            manifest["snapshot_db_sha256"], label="snapshot_db_sha256"
        ):
            raise PersonaSnapshotError("RN persona snapshot database hash differs from its manifest")
        source_sha = _sha256_text(manifest["source_db_sha256"], label="source_db_sha256")
        renderer = manifest["renderer"]
        if (
            not isinstance(renderer, Mapping)
            or set(renderer) != {"id", "sha256", "normalization"}
            or renderer.get("id") != RENDERER_ID
            or _sha256_text(renderer.get("sha256"), label="renderer.sha256") != persona_renderer_sha256()
            or renderer.get("normalization") != "NFC; LF-only; exactly-one-trailing-LF; fixed-sections"
        ):
            raise PersonaSnapshotError("RN persona snapshot renderer binding is invalid")
        rows = _read_agent_rows(db_path)
        raw_agents = manifest["agents"]
        if not isinstance(raw_agents, list) or int(manifest["agent_count"]) != len(rows) != len(raw_agents):
            raise PersonaSnapshotError("RN persona snapshot agent count differs from its database")
        personas: dict[str, FrozenPersona] = {}
        expected_rows: list[dict[str, Any]] = []
        for ordinal, (row, entry) in enumerate(zip(rows, raw_agents, strict=True), start=1):
            if not isinstance(entry, Mapping) or set(entry) != {
                "ordinal",
                "agent_id",
                "news_depth",
                "initial_cash",
                "structured_row_sha256",
                "persona_sha256",
            }:
                raise PersonaSnapshotError("RN persona snapshot agent entry has an invalid schema")
            prompt = str(row["persona_prompt"])
            parsed = parse_persona_v1(prompt)
            normalized = _normalized_agent_row(row)
            _assert_prompt_matches_row(parsed, normalized)
            rendered = render_persona_v1(normalized)
            if rendered != prompt:
                raise PersonaSnapshotError("RN persona snapshot prompt does not round-trip through render_persona_v1")
            structured_sha = scientific_sha256({key: normalized[key] for key in _STRUCTURED_COLUMNS})
            prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
            expected = {
                "ordinal": ordinal,
                "agent_id": normalized["agent_id"],
                "news_depth": normalized["news_depth"],
                "initial_cash": normalized["ini_cash"],
                "structured_row_sha256": structured_sha,
                "persona_sha256": prompt_sha,
            }
            if dict(entry) != expected:
                raise PersonaSnapshotError("RN persona snapshot manifest differs from its database row")
            if normalized["agent_id"] in personas:
                raise PersonaSnapshotError("RN persona snapshot has duplicate agent IDs")
            personas[normalized["agent_id"]] = FrozenPersona(
                agent_id=normalized["agent_id"],
                news_depth=normalized["news_depth"],
                initial_cash=normalized["ini_cash"],
                persona_prompt=prompt,
                persona_sha256=prompt_sha,
                structured_row_sha256=structured_sha,
            )
            expected_rows.append(expected)
        prompt_map_sha = scientific_sha256(expected_rows)
        if prompt_map_sha != _sha256_text(
            manifest["ordered_agent_prompt_map_sha256"], label="ordered_agent_prompt_map_sha256"
        ):
            raise PersonaSnapshotError("RN persona prompt map hash differs from its manifest")
        _assert_depth_counts(manifest["depth_counts"], personas.values())
        depth_manifest_sha = _verify_depth_manifest(
            root / DEPTH_MANIFEST_FILENAME,
            source_db_sha256=source_sha,
            expected_agents=expected_rows,
        )
        repair_manifest_sha = _verify_repair_manifest(
            root / REPAIR_MANIFEST_FILENAME,
            source_db_sha256=source_sha,
            snapshot_db_sha256=_sha256_text(manifest["snapshot_db_sha256"], label="snapshot_db_sha256"),
            ordered_agent_prompt_map_sha256=prompt_map_sha,
            agent_count=len(personas),
        )
        return cls(
            snapshot_dir=root,
            snapshot_db_path=db_path,
            manifest_sha256=scientific_sha256(manifest),
            source_db_sha256=source_sha,
            snapshot_db_sha256=_sha256_text(manifest["snapshot_db_sha256"], label="snapshot_db_sha256"),
            prompt_map_sha256=prompt_map_sha,
            depth_manifest_sha256=depth_manifest_sha,
            repair_manifest_sha256=repair_manifest_sha,
            personas=personas,
        )

    def persona(self, agent_id: str) -> FrozenPersona:
        try:
            return self.personas[agent_id]
        except KeyError as exc:
            raise PersonaSnapshotError(f"Agent is absent from the sealed persona snapshot: {agent_id}") from exc

    def assert_agent_set(self, agent_ids: Iterable[str]) -> None:
        requested = {str(agent_id) for agent_id in agent_ids}
        if requested != set(self.personas):
            raise PersonaSnapshotError("RN runtime agent IDs differ from the sealed persona snapshot")

    def seal_into(self, destination: Path | str) -> "SealedPersonaSnapshot":
        """Copy this verified snapshot into a fresh run-local input namespace."""
        target = Path(destination)
        if target.exists() or target.is_symlink():
            raise PersonaSnapshotError(f"RN persona sealing destination already exists: {target}")
        if not target.parent.exists() or not target.parent.is_dir():
            raise PersonaSnapshotError("RN persona sealing destination parent must already exist")
        # Re-read before copying, so a source changed after this instance was
        # created cannot be silently promoted into the run bundle.
        fresh = SealedPersonaSnapshot.load(self.snapshot_dir)
        if (
            fresh.snapshot_db_sha256 != self.snapshot_db_sha256
            or fresh.prompt_map_sha256 != self.prompt_map_sha256
            or fresh.depth_manifest_sha256 != self.depth_manifest_sha256
            or fresh.repair_manifest_sha256 != self.repair_manifest_sha256
        ):
            raise PersonaSnapshotError("RN persona source changed before it could be sealed")
        temporary = Path(tempfile.mkdtemp(prefix=".rn-persona-seal-", dir=target.parent))
        try:
            for name in (
                SNAPSHOT_DB_FILENAME,
                SNAPSHOT_MANIFEST_FILENAME,
                DEPTH_MANIFEST_FILENAME,
                REPAIR_MANIFEST_FILENAME,
            ):
                shutil.copyfile(self.snapshot_dir / name, temporary / name)
            sealed = SealedPersonaSnapshot.load(temporary)
            assert_persona_snapshot_identity(self, sealed)
            os.replace(temporary, target)
            return SealedPersonaSnapshot.load(target)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


def build_persona_snapshot(
    *,
    source_db_path: Path | str,
    snapshot_dir: Path | str,
    expected_agent_count: int | None = 100,
    expected_depth_counts: Mapping[int, int] | None = None,
) -> PersonaSnapshotArtifacts:
    """Create a fresh sealed snapshot without writing to the legacy source DB.

    The destination must not exist.  A temporary sibling directory is atomically
    renamed only after all manifests and byte-level validations succeed.
    """
    source = _require_regular_nonempty_file(Path(source_db_path), label="source persona DB")
    destination = Path(snapshot_dir)
    if destination.exists() or destination.is_symlink():
        raise PersonaSnapshotError(f"RN persona snapshot destination already exists: {destination}")
    if not destination.parent.exists() or not destination.parent.is_dir():
        raise PersonaSnapshotError("RN persona snapshot destination parent must already exist")
    source_rows = _read_agent_rows(source)
    _validate_source_rows(
        source_rows,
        expected_agent_count=expected_agent_count,
        expected_depth_counts=expected_depth_counts,
    )
    source_sha = _sha256_file(source)
    temp_root = Path(tempfile.mkdtemp(prefix=".rn-persona-snapshot-", dir=destination.parent))
    try:
        snapshot_db = temp_root / SNAPSHOT_DB_FILENAME
        _backup_sqlite(source, snapshot_db)
        snapshot_rows = _render_snapshot_database(snapshot_db, source_rows)
        _validate_snapshot_rows(source_rows, snapshot_rows)
        manifests = _build_manifests(
            source_rows=source_rows,
            snapshot_rows=snapshot_rows,
            source_db_sha256=source_sha,
            snapshot_db_sha256=_sha256_file(snapshot_db),
        )
        _write_canonical_json(temp_root / SNAPSHOT_MANIFEST_FILENAME, manifests["snapshot"])
        _write_canonical_json(temp_root / DEPTH_MANIFEST_FILENAME, manifests["depth"])
        _write_canonical_json(temp_root / REPAIR_MANIFEST_FILENAME, manifests["repair"])
        sealed = SealedPersonaSnapshot.load(temp_root)
        if expected_agent_count is not None and len(sealed.personas) != expected_agent_count:
            raise PersonaSnapshotError("Sealed persona snapshot count differs after write verification")
        os.replace(temp_root, destination)
        return PersonaSnapshotArtifacts(
            snapshot_dir=destination,
            snapshot_db_path=destination / SNAPSHOT_DB_FILENAME,
            snapshot_manifest_path=destination / SNAPSHOT_MANIFEST_FILENAME,
            depth_manifest_path=destination / DEPTH_MANIFEST_FILENAME,
            repair_manifest_path=destination / REPAIR_MANIFEST_FILENAME,
            source_db_sha256=source_sha,
            snapshot_db_sha256=sealed.snapshot_db_sha256,
            prompt_map_sha256=sealed.prompt_map_sha256,
        )
    except BaseException:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise


def assert_persona_snapshot_identity(
    left: SealedPersonaSnapshot,
    right: SealedPersonaSnapshot,
) -> None:
    """Require arm snapshots to have byte-identical persona prompt maps."""
    if left.prompt_map_sha256 != right.prompt_map_sha256 or set(left.personas) != set(right.personas):
        raise PersonaSnapshotError("RN arms do not have the same sealed persona prompt map")
    for agent_id in left.personas:
        if left.persona(agent_id) != right.persona(agent_id):
            raise PersonaSnapshotError(f"RN arms have different persona bytes for {agent_id}")


def _require_regular_nonempty_file(path: Path, *, label: str) -> Path:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise PersonaSnapshotError(f"{label} does not exist: {path}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode) or path.stat().st_size <= 0:
        raise PersonaSnapshotError(f"{label} must be a non-empty regular file: {path}")
    return path


def _read_agent_rows(path: Path) -> list[dict[str, Any]]:
    uri = f"file:{path.absolute().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        columns = [str(row["name"]) for row in connection.execute("PRAGMA table_info(agents)")]
        if tuple(columns) != AGENT_COLUMNS:
            raise PersonaSnapshotError("agents table columns differ from the required legacy cohort schema")
        rows = [dict(row) for row in connection.execute("SELECT * FROM agents ORDER BY agent_id COLLATE BINARY")]
    except sqlite3.Error as exc:
        raise PersonaSnapshotError(f"Cannot read RN persona source database: {exc}") from exc
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    return rows


def _validate_source_rows(
    rows: list[dict[str, Any]],
    *,
    expected_agent_count: int | None,
    expected_depth_counts: Mapping[int, int] | None,
) -> None:
    if not rows:
        raise PersonaSnapshotError("RN persona source database has no agents")
    if expected_agent_count is not None and len(rows) != expected_agent_count:
        raise PersonaSnapshotError(
            f"RN persona source count differs from the frozen requirement: {len(rows)} != {expected_agent_count}"
        )
    normalized = [_normalized_agent_row(row) for row in rows]
    agent_ids = [row["agent_id"] for row in normalized]
    source_user_ids = [row["source_user_id"] for row in normalized]
    if len(agent_ids) != len(set(agent_ids)) or len(source_user_ids) != len(set(source_user_ids)):
        raise PersonaSnapshotError("RN persona source has duplicate agent_id or source_user_id")
    if expected_depth_counts is not None:
        actual = {int(key): int(value) for key, value in Counter(row["news_depth"] for row in normalized).items()}
        wanted = {int(key): int(value) for key, value in expected_depth_counts.items()}
        if actual != wanted:
            raise PersonaSnapshotError(f"RN persona source depth counts differ: {actual} != {wanted}")


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_connection = sqlite3.connect(f"file:{source.absolute().as_posix()}?mode=ro", uri=True)
    try:
        target_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
    except sqlite3.Error as exc:
        raise PersonaSnapshotError(f"Cannot create RN persona snapshot database: {exc}") from exc
    finally:
        source_connection.close()


def _render_snapshot_database(snapshot_db: Path, source_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        connection = sqlite3.connect(snapshot_db)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=DELETE")
        for row in source_rows:
            rendered = render_persona_v1(row)
            connection.execute(
                "UPDATE agents SET persona_prompt = ? WHERE agent_id = ?",
                (rendered, row["agent_id"]),
            )
        connection.commit()
        connection.execute("VACUUM")
        rows = [dict(row) for row in connection.execute("SELECT * FROM agents ORDER BY agent_id COLLATE BINARY")]
    except sqlite3.Error as exc:
        raise PersonaSnapshotError(f"Cannot render RN persona snapshot database: {exc}") from exc
    finally:
        try:
            connection.close()
        except UnboundLocalError:
            pass
    return rows


def _validate_snapshot_rows(source_rows: list[dict[str, Any]], snapshot_rows: list[dict[str, Any]]) -> None:
    if len(source_rows) != len(snapshot_rows):
        raise PersonaSnapshotError("RN persona snapshot changed its cohort row count")
    for source, snapshot in zip(source_rows, snapshot_rows, strict=True):
        if source["agent_id"] != snapshot["agent_id"]:
            raise PersonaSnapshotError("RN persona snapshot changed agent ordering or IDs")
        changed = [
            column
            for column in _STRUCTURED_COLUMNS
            if source[column] != snapshot[column]
        ]
        if changed:
            raise PersonaSnapshotError(
                "RN persona snapshot changed non-prompt structured fields: " + ",".join(changed)
            )
        normalized = _normalized_agent_row(snapshot)
        parsed = parse_persona_v1(str(snapshot["persona_prompt"]))
        _assert_prompt_matches_row(parsed, normalized)
        if render_persona_v1(normalized) != snapshot["persona_prompt"]:
            raise PersonaSnapshotError("RN persona snapshot prompt failed canonical round-trip")


def _assert_prompt_matches_row(parsed: Mapping[str, Any], row: Mapping[str, Any]) -> None:
    compared = {
        "gender",
        "age",
        "age_group",
        "location",
        "user_type",
        "bh_disposition_effect_category",
        "bh_lottery_preference_category",
        "bh_total_return_category",
        "bh_underdiversification_category",
        "strategy",
        "trad_pro",
        "fol_ind",
        "ini_cash",
        "news_depth",
    }
    if {key: row[key] for key in compared} != {key: parsed[key] for key in compared}:
        raise PersonaSnapshotError("RN canonical persona prompt disagrees with structured row fields")


def _legacy_prompt_depth(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value)
    matches = {
        0: "헤드라인만 훑고",
        1: "10개 요약본",
        2: "최근 7일 뉴스",
    }
    found = [depth for depth, marker in matches.items() if marker in text]
    return found[0] if len(found) == 1 else None


def _build_manifests(
    *,
    source_rows: list[dict[str, Any]],
    snapshot_rows: list[dict[str, Any]],
    source_db_sha256: str,
    snapshot_db_sha256: str,
) -> dict[str, dict[str, Any]]:
    renderer_sha = persona_renderer_sha256()
    agents: list[dict[str, Any]] = []
    repairs: list[dict[str, Any]] = []
    before_mismatch = 0
    after_mismatch = 0
    non_prompt_changes = 0
    for ordinal, (source, snapshot) in enumerate(zip(source_rows, snapshot_rows, strict=True), start=1):
        normalized = _normalized_agent_row(snapshot)
        prompt = str(snapshot["persona_prompt"])
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        structured_sha = scientific_sha256({key: normalized[key] for key in _STRUCTURED_COLUMNS})
        source_depth = _legacy_prompt_depth(source["persona_prompt"])
        if source_depth != normalized["news_depth"]:
            before_mismatch += 1
        parsed = parse_persona_v1(prompt)
        if parsed["news_depth"] != normalized["news_depth"]:
            after_mismatch += 1
        changed_fields = [column for column in _STRUCTURED_COLUMNS if source[column] != snapshot[column]]
        non_prompt_changes += len(changed_fields)
        entry = {
            "ordinal": ordinal,
            "agent_id": normalized["agent_id"],
            "news_depth": normalized["news_depth"],
            "initial_cash": normalized["ini_cash"],
            "structured_row_sha256": structured_sha,
            "persona_sha256": prompt_sha,
        }
        agents.append(entry)
        repairs.append(
            {
                "agent_id": normalized["agent_id"],
                "old_persona_sha256": hashlib.sha256(
                    str(source["persona_prompt"]).encode("utf-8")
                ).hexdigest(),
                "new_persona_sha256": prompt_sha,
                "source_news_depth": normalized["news_depth"],
                "legacy_prompt_news_depth": source_depth,
                "post_prompt_news_depth": parsed["news_depth"],
                "non_prompt_changed_fields": changed_fields,
            }
        )
    depth_counts = {str(depth): count for depth, count in sorted(Counter(item["news_depth"] for item in agents).items())}
    ordered_map_sha = scientific_sha256(agents)
    depth_entries = [
        {
            "agent_id": item["agent_id"],
            "news_depth": item["news_depth"],
            "persona_sha256": item["persona_sha256"],
        }
        for item in agents
    ]
    snapshot = {
        "artifact_type": "rn_persona_snapshot",
        "version": SNAPSHOT_VERSION,
        "source_db_sha256": source_db_sha256,
        "snapshot_db_sha256": snapshot_db_sha256,
        "renderer": {
            "id": RENDERER_ID,
            "sha256": renderer_sha,
            "normalization": "NFC; LF-only; exactly-one-trailing-LF; fixed-sections",
        },
        "agent_count": len(agents),
        "depth_counts": depth_counts,
        "ordered_agent_prompt_map_sha256": ordered_map_sha,
        "agents": agents,
    }
    depth = {
        "artifact_type": "rn_persona_depth_manifest",
        "version": "rn-persona-depth-v1",
        "source_db_sha256": source_db_sha256,
        "ordered_agent_depth_persona_map_sha256": scientific_sha256(depth_entries),
        "agents": depth_entries,
    }
    repair = {
        "artifact_type": "rn_persona_repair_manifest",
        "version": "rn-persona-repair-v1",
        "source_db_sha256": source_db_sha256,
        "snapshot_db_sha256": snapshot_db_sha256,
        "renderer_sha256": renderer_sha,
        "agent_count": len(agents),
        "legacy_prompt_depth_mismatch_count": before_mismatch,
        "post_repair_prompt_depth_mismatch_count": after_mismatch,
        "depth_changed_agent_count": 0,
        "non_prompt_structured_field_change_count": non_prompt_changes,
        "canonical_roundtrip_count": len(agents),
        "ordered_agent_prompt_map_sha256": ordered_map_sha,
        "repairs": repairs,
    }
    return {"snapshot": snapshot, "depth": depth, "repair": repair}


def _write_canonical_json(path: Path, payload: Mapping[str, Any]) -> None:
    rendered = canonical_json(dict(payload))
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(rendered)
        handle.write("\n")


def _read_canonical_manifest(path: Path, *, label: str) -> Mapping[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise PersonaSnapshotError(f"{label} is unreadable") from exc
    if not isinstance(value, Mapping) or raw != canonical_json(dict(value)) + "\n":
        raise PersonaSnapshotError(f"{label} is not canonical JSON with one trailing LF")
    return value


def _verify_depth_manifest(
    path: Path,
    *,
    source_db_sha256: str,
    expected_agents: list[dict[str, Any]],
) -> str:
    value = _read_canonical_manifest(path, label="RN persona depth manifest")
    expected_keys = {
        "artifact_type",
        "version",
        "source_db_sha256",
        "ordered_agent_depth_persona_map_sha256",
        "agents",
    }
    if set(value) != expected_keys:
        raise PersonaSnapshotError("RN persona depth manifest has an invalid schema")
    depth_entries = [
        {
            "agent_id": item["agent_id"],
            "news_depth": item["news_depth"],
            "persona_sha256": item["persona_sha256"],
        }
        for item in expected_agents
    ]
    if (
        value.get("artifact_type") != "rn_persona_depth_manifest"
        or value.get("version") != "rn-persona-depth-v1"
        or _sha256_text(value.get("source_db_sha256"), label="depth.source_db_sha256")
        != source_db_sha256
        or value.get("agents") != depth_entries
        or _sha256_text(
            value.get("ordered_agent_depth_persona_map_sha256"),
            label="ordered_agent_depth_persona_map_sha256",
        )
        != scientific_sha256(depth_entries)
    ):
        raise PersonaSnapshotError("RN persona depth manifest does not match the sealed snapshot")
    return scientific_sha256(dict(value))


def _verify_repair_manifest(
    path: Path,
    *,
    source_db_sha256: str,
    snapshot_db_sha256: str,
    ordered_agent_prompt_map_sha256: str,
    agent_count: int,
) -> str:
    value = _read_canonical_manifest(path, label="RN persona repair manifest")
    expected_keys = {
        "artifact_type",
        "version",
        "source_db_sha256",
        "snapshot_db_sha256",
        "renderer_sha256",
        "agent_count",
        "legacy_prompt_depth_mismatch_count",
        "post_repair_prompt_depth_mismatch_count",
        "depth_changed_agent_count",
        "non_prompt_structured_field_change_count",
        "canonical_roundtrip_count",
        "ordered_agent_prompt_map_sha256",
        "repairs",
    }
    if set(value) != expected_keys:
        raise PersonaSnapshotError("RN persona repair manifest has an invalid schema")
    if (
        value.get("artifact_type") != "rn_persona_repair_manifest"
        or value.get("version") != "rn-persona-repair-v1"
        or _sha256_text(value.get("source_db_sha256"), label="repair.source_db_sha256")
        != source_db_sha256
        or _sha256_text(value.get("snapshot_db_sha256"), label="repair.snapshot_db_sha256")
        != snapshot_db_sha256
        or _sha256_text(value.get("renderer_sha256"), label="repair.renderer_sha256")
        != persona_renderer_sha256()
        or value.get("agent_count") != agent_count
        or value.get("post_repair_prompt_depth_mismatch_count") != 0
        or value.get("depth_changed_agent_count") != 0
        or value.get("non_prompt_structured_field_change_count") != 0
        or value.get("canonical_roundtrip_count") != agent_count
        or _sha256_text(
            value.get("ordered_agent_prompt_map_sha256"), label="repair.ordered_agent_prompt_map_sha256"
        )
        != ordered_agent_prompt_map_sha256
    ):
        raise PersonaSnapshotError("RN persona repair manifest does not match the sealed snapshot")
    legacy_mismatch = value.get("legacy_prompt_depth_mismatch_count")
    if isinstance(legacy_mismatch, bool) or not isinstance(legacy_mismatch, int) or legacy_mismatch < 0:
        raise PersonaSnapshotError("RN persona repair manifest has an invalid legacy mismatch count")
    repairs = value.get("repairs")
    if not isinstance(repairs, list) or len(repairs) != agent_count:
        raise PersonaSnapshotError("RN persona repair manifest has an invalid repair row count")
    expected_repair_keys = {
        "agent_id",
        "old_persona_sha256",
        "new_persona_sha256",
        "source_news_depth",
        "legacy_prompt_news_depth",
        "post_prompt_news_depth",
        "non_prompt_changed_fields",
    }
    seen: set[str] = set()
    for repair in repairs:
        if not isinstance(repair, Mapping) or set(repair) != expected_repair_keys:
            raise PersonaSnapshotError("RN persona repair manifest has an invalid repair entry")
        agent_id = _canonical_text(repair["agent_id"], label="repair.agent_id")
        if agent_id in seen:
            raise PersonaSnapshotError("RN persona repair manifest repeats an agent")
        seen.add(agent_id)
        _sha256_text(repair["old_persona_sha256"], label="repair.old_persona_sha256")
        _sha256_text(repair["new_persona_sha256"], label="repair.new_persona_sha256")
        if repair["source_news_depth"] not in {0, 1, 2} or repair["post_prompt_news_depth"] not in {0, 1, 2}:
            raise PersonaSnapshotError("RN persona repair manifest has an invalid depth")
        legacy_depth = repair["legacy_prompt_news_depth"]
        if legacy_depth is not None and legacy_depth not in {0, 1, 2}:
            raise PersonaSnapshotError("RN persona repair manifest has an invalid legacy prompt depth")
        if repair["post_prompt_news_depth"] != repair["source_news_depth"]:
            raise PersonaSnapshotError("RN persona repair manifest leaves a depth mismatch")
        if repair["non_prompt_changed_fields"] != []:
            raise PersonaSnapshotError("RN persona repair manifest reports a structured field mutation")
    return scientific_sha256(dict(value))


def _sha256_text(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_HEX_LENGTH:
        raise PersonaSnapshotError(f"{label} must be a SHA-256 digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise PersonaSnapshotError(f"{label} must be a SHA-256 digest") from exc
    return value.lower()


def _assert_depth_counts(value: Any, personas: Iterable[FrozenPersona]) -> None:
    if not isinstance(value, Mapping):
        raise PersonaSnapshotError("RN persona snapshot depth_counts must be an object")
    expected = {str(depth): count for depth, count in sorted(Counter(item.news_depth for item in personas).items())}
    normalized = {str(key): int(count) for key, count in value.items()}
    if normalized != expected:
        raise PersonaSnapshotError("RN persona snapshot depth counts differ from its database")
