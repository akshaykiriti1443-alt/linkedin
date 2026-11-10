"""WP5: AI-facing models, portable schemas, Short/Few truncation, desktop argv sizes, taxonomy (12.1)."""

import json

import pytest
from pydantic import ValidationError

from reelvault import ai, members_worker, schema_members as S, taxonomy

ALLOWED = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "anyOf",
    "$defs",
    "$ref",
    "description",
    "const",
}
BANNED = {
    "maxLength",
    "minLength",
    "maxItems",
    "minItems",
    "pattern",
    "format",
    "default",
    "title",
    "examples",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "uniqueItems",
}


def keywords(node, in_props=False, acc=None):
    acc = set() if acc is None else acc
    if isinstance(node, list):
        for v in node:
            keywords(v, False, acc)
    elif isinstance(node, dict):
        for k, v in node.items():
            if not in_props:
                acc.add(k)
            keywords(v, (not in_props) and k in ("properties", "$defs"), acc)
    return acc


@pytest.mark.parametrize("name", sorted(S.MODELS))
@pytest.mark.parametrize("provider", ["anthropic", "openai", "openai_compat", "gemini"])
def test_portable_schema_only_allowlisted_keywords(name, provider):
    s = ai.portable_schema(S.MODELS[name], provider)
    kw = keywords(s)
    assert kw <= ALLOWED, kw - ALLOWED
    assert not kw & BANNED
    if provider == "gemini":
        assert "$defs" not in kw and "$ref" not in kw and "const" not in kw


def test_portable_schema_is_strict_and_keeps_field_names():
    s = ai.portable_schema(S.ReelScriptPack, "anthropic")
    script = s["$defs"]["ReelScript"]
    assert script["additionalProperties"] is False
    assert set(script["required"]) == set(script["properties"])
    assert "format" in script["properties"] and "title" in script["properties"]  # field names survive
    eb = s["$defs"]["EditorBrief"]["properties"]["format"]
    assert set(eb) <= {"enum", "type", "$ref", "description", "anyOf"}
    ma = ai.portable_schema(S.MetaAdPack, "gemini")
    assert "description" in ma["properties"]["ads"]["items"]["properties"]  # a field literally named description


def _tag(**kw):
    t = {
        "reel_id": "r",
        "visual_format": "VF01",
        "visual_format_other": "",
        "visual_format_2": [],
        "visual_confidence": 0.5,
        "visual_evidence": [],
        "visual_subject": "x",
        "recipe": {
            "headline_box": False,
            "caption_style": "none",
            "caption_look": "",
            "face_bubble": False,
            "broll_share": "none",
            "setting": "desk",
            "framing": "close",
        },
        "sound_primary": "SD01",
        "sound_secondary": [],
        "topics": [],
        "new_topic": "",
        "hook_type": "question",
        "hook_line": "",
        "cta_type": "none",
        "has_text_hook": False,
        "one_line": "",
    }
    t.update(kw)
    return t


def test_short_and_few_trim_instead_of_failing():
    long_line = "word " * 26  # 130 chars
    q = S.QuickTags.model_validate(
        _tag(
            one_line=long_line,
            visual_format_2=["VF02", "VF03", "VF04"],
            visual_evidence=list(range(20)),
            visual_confidence=3,
        )
    )
    assert len(q.one_line) <= 120 and q.one_line.endswith("…")
    assert q.visual_format_2 == ["VF02", "VF03"] and len(q.visual_evidence) == 6
    assert q.visual_confidence == 1.0
    assert S.trim_text("short", 10) == "short" and S.trim_text(None, 5) == ""


def test_structural_errors_still_fail():
    with pytest.raises(ValidationError):
        S.QuickTags.model_validate(_tag(visual_format="VF99"))
    with pytest.raises(ValidationError):
        S.QuickTags.model_validate({k: v for k, v in _tag().items() if k != "hook_type"})
    with pytest.raises(ValidationError):
        S.QuickTags.model_validate(_tag(has_text_hook={"a": 1}))


def test_counts_that_are_the_point():
    from _wp5_support import ad, plan_chunk

    a = ad()
    a["hook_test"]["hooks"] = a["hook_test"]["hooks"][:4]
    with pytest.raises(ValidationError, match="exactly 5"):
        S.MetaAd.model_validate(a)
    a = ad()
    a["hook_test"]["hooks"][1]["angle"] = "pain"
    with pytest.raises(ValidationError, match="5 different angles"):
        S.MetaAd.model_validate(a)
    good = S.MetaAd.model_validate(ad())
    assert [h.id for h in good.hook_test.hooks] == ["H1", "H2", "H3", "H4", "H5"]
    assert good.ad_15.length_s == 15 and good.ad_30.length_s == 30
    ch = plan_chunk(0, "- fmt:fmt_x")
    ch["weeks"][0]["days"] = ch["weeks"][0]["days"][:6]
    with pytest.raises(ValidationError, match="exactly 7 days"):
        S.ContentPlanChunk.model_validate(ch)


def test_weaknesses_renumbered_and_slugs_normalized():
    from _wp5_support import breakdown

    bd = S.ReelBreakdown.model_validate(breakdown("reel_x"))
    assert [w.id for w in bd.weaknesses] == ["W1", "W2"]
    t = S.TopicList.model_validate(
        {"topics": [{"slug": "Canva Templates!", "label": "x", "description": "", "examples": []}]}
    )
    assert t.topics[0].slug == "canva-templates"


def test_field_guide_carries_limits():
    g = S.field_guide(S.QuickTagBatch)
    assert "tags[].one_line" in g and "≤ 120 chars" in g and "at most 2 items" in g
    g2 = S.field_guide(S.FormatGuideBatch)
    assert "guides[].why_it_works" in g2 and "each ≤ 160 chars" in g2


@pytest.mark.parametrize("name", sorted(S.MODELS))
def test_desktop_argv_fits_or_uses_schema_file(name, tmp_path):
    """D23: every model the desktop worker can receive either fits 6,000 argv characters or goes through
    schema.json; run_task picks the file path for exactly the ones that don't fit."""
    model, schema = members_worker.schema_for(name)
    n = members_worker.argv_length(members_worker.INSTRUCTION, schema)
    seen = {}

    def runner(prompt, instruction, system, cwd, schema_arg, read_files, model_arg, effort):
        seen.update(schema=schema_arg, instruction=instruction, file=(cwd / "schema.json").exists())
        raise members_worker.WorkerError("stop")

    with pytest.raises(members_worker.WorkerError):
        members_worker.run_task(
            {"schema_name": name, "prompt": "p", "system": "s", "images": []}, tmp_path, runner=runner
        )
    if n > members_worker.ARGV_LIMIT:
        assert seen["schema"] is None and seen["file"] and "schema.json" in seen["instruction"]
        assert members_worker.argv_length(members_worker.INSTRUCTION, None) < members_worker.ARGV_LIMIT
    else:
        assert seen["schema"] == schema and not seen["file"]


def test_taxonomy_literals_match_json():
    t = json.loads(taxonomy.PATH.read_text())
    assert list(taxonomy.VF_IDS) == [x["id"] for x in t["visual_format"]] == [f"VF{i:02d}" for i in range(1, 21)]
    assert list(taxonomy.SD_IDS) == [f"SD{i:02d}" for i in range(1, 17)]
    assert list(taxonomy.EP_IDS) == [f"EP{i:02d}" for i in range(1, 20)]
    enum = S.QuickTags.model_json_schema()["properties"]["visual_format"]["enum"]
    assert enum == list(taxonomy.VF_IDS) + ["other"]
    assert taxonomy.needs("VF12") == ["on_camera", "location", "second_person"] and taxonomy.needs("VF07") == []
    assert taxonomy.legacy_vf("tutorial") == "VF13" and taxonomy.label("VF04").startswith("Green screen")
    assert t["local_rules"], "WP3's local_rules block must be kept"
    for kind in ("visual", "sound", "edit", "lists"):
        assert taxonomy.prompt_block(kind)
    assert "Precedence" in taxonomy.prompt_block("visual")
