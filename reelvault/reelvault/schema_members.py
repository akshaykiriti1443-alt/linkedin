"""Pydantic models for every AI-facing output of ReelVault Members (DESIGN-SAAS.md 6.5, 6.8, 6.9, 8, 9.3). WP5.

Rules for AI-facing models (7.1.1, D7):
- Text limits are `Short[n]` (trims on a word boundary and adds "…") and list limits are `Few[T, n]` (keeps the first
  n). Both are written into the field guide text, never into the JSON schema, so every provider accepts the schema
  and an overshoot is trimmed, not rejected.
- No `Field(max_length=...)` and no defaults. Validation fails only on structural errors (wrong enum, missing field,
  wrong type, a wrong count where the count is the point). Cross-checks against the member's data (batch reel ids,
  pains, cards, weaknesses) live in `server/work.py` `save_result`.

    MODELS[name] -> model class (AiTask.schema_name), field_guide(model_cls) -> str, model_for(schema_name)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from . import taxonomy
from .schema import CTA, Hook, Skeleton

VFId = taxonomy.VFId
SDId = taxonomy.SDId
HookType = taxonomy.HookType
CTAType = taxonomy.CTAType
CaptionStyle = taxonomy.CaptionStyle
CueKind = taxonomy.CueKind
NeedId = taxonomy.Need
VFOrNone = Literal[taxonomy.VF_CHOICES + ("none",)]  # type: ignore[valid-type]

SCHEMA_VERSION = 1
ELLIPSIS = "…"


# ============================================================================================== Short / Few


@dataclass(frozen=True)
class Limit:
    """Metadata marker read by field_guide(): '≤ n chars' or 'at most n items'."""

    n: int
    unit: str  # "chars" | "items"

    def text(self) -> str:
        return f"≤ {self.n} chars" if self.unit == "chars" else f"at most {self.n} items"


def trim_text(v: Any, n: int) -> Any:
    """Cut a string to n characters on a word boundary, ending with '…'. Non-strings pass through (None -> '')."""
    if v is None:
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        return v
    v = v.strip()
    if len(v) <= n:
        return v
    cut = v[: max(1, n - 1)]
    sp = cut.rfind(" ")
    if sp >= int(n * 0.6):
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-–—") + ELLIPSIS


def trim(n: int):
    return lambda v: trim_text(v, n)


def cap(n: int):
    def run(v: Any) -> Any:
        if v is None:
            return []
        return list(v)[:n] if isinstance(v, (list, tuple)) else v

    return run


class Short:
    """Short[120] = a string trimmed to 120 characters (never a validation error)."""

    def __class_getitem__(cls, n: int):
        return Annotated[str, BeforeValidator(trim(int(n))), Limit(int(n), "chars")]


class Few:
    """Few[T, 5] = a list that keeps its first 5 items (never a validation error)."""

    def __class_getitem__(cls, params):
        t, n = params
        return Annotated[list[t], BeforeValidator(cap(int(n))), Limit(int(n), "items")]  # type: ignore[valid-type]


def D(text: str) -> Any:
    """Field description (shown in the field guide). Never a default: every AI-facing field is required."""
    return Field(description=text)


def clamp01(v: Any) -> Any:
    try:
        return min(1.0, max(0.0, float(v)))
    except (TypeError, ValueError):
        return v


Unit01 = Annotated[float, BeforeValidator(clamp01), Limit(1, "unit")]


class AIModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


# ============================================================================================== 6.5 quick tags


class FormatRecipe(AIModel):
    """The visible details of the format; the base of named sub-formats (6.9)."""

    headline_box: bool = D("A persistent hook text box, usually at the top, from about 0 s (EP05).")
    caption_style: CaptionStyle = D("How spoken words appear as captions.")
    caption_look: Short[60] = D(
        "Font look, case, color and position, e.g. 'yellow bold all-caps, centre'. Empty when none."
    )
    face_bubble: bool = D("A small face cam over a screen or B-roll (EP17).")
    broll_share: Literal["none", "light", "medium", "heavy"] = D("Share of body frames that are B-roll (EP06).")
    setting: Literal[
        "studio", "desk", "car", "walk_and_talk", "kitchen", "stage", "event", "outdoors", "screen_only", "other"
    ] = D("Where it is filmed.")
    framing: Literal["close", "medium", "wide", "none"] = D(
        "Shot size of the person; 'none' when no person is on screen."
    )


class QuickTags(AIModel):
    reel_id: str = D("Copy the reel id exactly. It must be one of the ids in this batch.")
    visual_format: VFId = D("Primary visual format id, from the frames and VisualStats, never from the script.")
    visual_format_other: Short[40] = D("Empty unless visual_format is 'other': then a short label.")
    visual_format_2: Few[VFId, 2] = D("0-2 secondary visual format ids.")
    visual_confidence: Unit01 = D("How sure you are of the visual format, 0 to 1.")
    visual_evidence: Few[int, 6] = D("Tile numbers that show the format.")
    visual_subject: Short[80] = D("What is literally on screen, e.g. 'Figma file being edited'.")
    recipe: FormatRecipe = D("The visible details of the format.")
    sound_primary: SDId = D("Primary sound design id.")
    sound_secondary: Few[SDId, 3] = D("0-3 secondary sound design ids.")
    topics: Few[str, 3] = D("0-3 slugs from the member's active topic list. [] when nothing fits (Unsorted).")
    new_topic: Short[40] = D("Empty, or one suggested new topic label for what the reel SHOWS.")
    hook_type: HookType = D("The hook pattern.")
    hook_line: Short[160] = D("The first spoken sentence or the on-screen hook text, as seen or heard.")
    cta_type: CTAType = D("The call to action type.")
    has_text_hook: bool = D("On-screen text in the first 2 seconds.")
    one_line: Short[120] = D("What the reel does, in one line.")


class QuickTagBatch(AIModel):
    tags: list[QuickTags] = D("Exactly one entry per reel id in the batch, each id once.")


# ============================================================================================== 6.8 deep breakdown


class FormatRead(AIModel):
    primary: VFId = D("Primary visual format id.")
    secondary: Few[VFId, 2] = D("0-2 secondary visual format ids.")
    other_label: Short[40] = D("Empty unless primary is 'other'.")
    confidence: Unit01 = D("0 to 1.")
    evidence_frames: Few[int, 10] = D("Frame numbers that show the format.")
    why: Short[300] = D("What in the frames shows this format.")
    recipe: Short[500] = D("How to shoot this format, step by step.")


class SoundCue(AIModel):
    t: float = D("Seconds, taken from the local cue list. Never invent a cue.")
    kind: CueKind = D("Cue kind.")
    what: Short[80] = D("What you hear.")
    purpose: Short[80] = D("Why it is there, e.g. 'marks the cut to proof'.")


class SoundRead(AIModel):
    primary: SDId = D("Primary sound design id.")
    secondary: Few[SDId, 3] = D("0-3 secondary sound design ids.")
    evidence_seconds: Few[float, 10] = D("Seconds on the timeline that show the pattern.")
    music: Short[160] = D("Music bed, mood, level, licensed vs original (from ig_audio).")
    cues: Few[SoundCue, 25] = D("Cues with times from the local cue list.")
    takeaway: Short[240] = D("What to copy about the sound.")


class EditRead(AIModel):
    jump_cuts: bool = D("Same background and person before and after cuts (EP02).")
    zoom_punch_ins: Literal["none", "few", "many"] = D("EP03.")
    caption_style: CaptionStyle = D("EP04.")
    caption_look: Short[120] = D("Font look, case, color, position.")
    headline_box: bool = D("EP05.")
    broll_share: Literal["none", "light", "medium", "heavy"] = D("EP06.")
    visual_hook: Literal[
        "motion_start",
        "prop_or_object",
        "result_first",
        "text_only",
        "face_closeup",
        "pattern_interrupt",
        "screenshot_proof",
    ] = D("What happens in the first second (EP07).")
    graphics: Few[Short[60], 8] = D("Graphics and stickers (EP08).")
    proof_inserts: bool = D("Screenshots of results, dashboards, DMs or reviews (EP09).")
    transitions: Few[Short[40], 6] = D("hard_cut, whip, zoom, glitch, match_cut, masked_reveal (EP10).")
    framing: Literal["close", "medium", "wide"] = D("EP11.")
    setting: Short[40] = D("EP12.")
    look: Short[80] = D("Color and look (EP13).")
    loop_ending: bool = D("The last line flows back into the first (EP15).")
    rehooks: int = D("Count of re-hooks in the body (EP16).")
    face_bubble: bool = D("EP17.")
    speed_effects: Few[Short[40], 5] = D("EP18.")
    cta_visual: Short[120] = D("How the CTA shows on screen and when (EP19).")


class Weakness(AIModel):
    id: Literal["W1", "W2", "W3", "W4", "W5"] = D("W1, W2, ... in order.")
    kind: Literal[
        "slow_hook",
        "weak_visual_hook",
        "no_proof",
        "generic_cta",
        "long_middle",
        "no_rehook",
        "unclear_offer",
        "hard_to_follow",
        "poor_audio",
        "dated_look",
        "other",
    ] = D("What kind of weakness.")
    note: Short[160] = D("Plain words, e.g. 'Hook takes 4 s to get to the point'.")
    at_s: float | None = D("Where it happens in seconds, or null.")


class ReelBreakdown(AIModel):
    schema_version: Literal[1] = D("Always 1.")
    reel_id: str = D("Copy the reel id exactly.")
    title: Short[80] = D("10 words or less.")
    summary: Short[600] = D("2-3 sentences.")
    topics: Few[str, 3] = D("0-3 active topic slugs; [] = Unsorted.")
    format: FormatRead = D("The visual format read from the frames.")
    skeleton: Skeleton = D("Hook, body beats, CTA, pacing and retention devices.")
    sound: SoundRead = D("The sound design, from the cue list.")
    edit: EditRead = D("Visual edit fields read from the frames.")
    why_it_worked: Few[Short[200], 5] = D("3-5 short points tied to evidence (format, hook, sound, topic, timing).")
    what_to_steal: Few[Short[200], 5] = D("3-5 concrete moves for the member's niche.")
    what_to_avoid: Few[Short[200], 3] = D("0-3 things not to copy.")
    weaknesses: Few[Weakness, 5] = D("2-5 weak spots in the ORIGINAL, so a remake can beat it.")
    fill_in_template: Short[1500] = D("A reusable script with [BRACKETED] slots, built on the skeleton.")
    hook_formula: Short[200] = D("One line with [SLOTS].")
    confidence_notes: Short[400] = D("What you could not tell. Empty if nothing.")

    @field_validator("weaknesses")
    @classmethod
    def _renumber(cls, v):
        for i, w in enumerate(v):
            w.id = f"W{i + 1}"
        return v


# ============================================================================================== 6.9 format names


class FormatGuide(AIModel):
    format_id: str = D("Copy the format id exactly.")
    name: Short[70] = D("e.g. 'Desk talking head + top headline box + yellow word captions'.")
    what_it_is: Short[300] = D("What this format is, in plain words.")
    why_it_works: Few[Short[160], 4] = D("Why it works.")
    shoot_steps: Few[Short[160], 8] = D("Camera, framing, setting, props, what to film.")
    edit_steps: Few[Short[160], 8] = D("Captions, headline box, cuts per 10 s, zooms, B-roll, graphics.")
    sound_recipe: Few[Short[160], 5] = D("Bed and ducking, SFX on which moments, silence beats (from the cue data).")
    needs: Few[NeedId, 6] = D("What the member must be able to do.")
    time_to_make: Literal["under_1h", "1_3h", "half_day"] = D("Time to make one.")


class FormatGuideBatch(AIModel):
    guides: list[FormatGuide] = D("Exactly one guide per format id in the task, each once.")


# ============================================================================================== 9.3 profile, 10.4 topics


class DraftAnswer(AIModel):
    value: Short[2000] = D("The answer for text questions. Empty when the sources don't say.")
    items: Few[Short[300], 10] = D("The answer for list questions (q3, q6, q14). [] otherwise.")
    source_ids: Few[str, 5] = D("Ids of the sources the answer comes from.")
    confidence: Unit01 = D("0 to 1. Under 0.5 = a guess, shown as a suggestion.")
    note: Short[200] = D("Short note for the member, e.g. 'From your About page'.")


class DraftAnswers(AIModel):
    q1: DraftAnswer = D("Who do you help?")
    q2: DraftAnswer = D("Your best client so far.")
    q3: DraftAnswer = D("Their top 3 problems, in their words (list).")
    q4: DraftAnswer = D("What have they already tried that didn't work?")
    q5: DraftAnswer = D("What do they really want?")
    q6: DraftAnswer = D("Why do people say no or not now? (list)")
    q7: DraftAnswer = D("What do you sell?")
    q8: DraftAnswer = D("Your proof.")
    q9: DraftAnswer = D("What makes you different?")
    q10: DraftAnswer = D("How do you talk?")
    q11: DraftAnswer = D("Where do you post, and what should a viewer do next?")
    q12: DraftAnswer = D("What can you actually make?")
    q13: DraftAnswer = D("Hard no's.")
    q14: DraftAnswer = D("Competitors or role models (list of Instagram handles).")


class ProfileDraft(AIModel):
    answers: DraftAnswers = D(
        "One draft answer per question. Leave unknown answers empty; never guess q3, q4, q6, q10, q12, q13."
    )
    competitor_handles: Few[Short[30], 5] = D("Instagram handles named in the sources, without @.")
    niche_label: Short[80] = D("e.g. 'Meta ads for local dentists'.")
    search_terms: Few[Short[60], 6] = D("Instagram search terms that find accounts in this niche.")


class TopicItem(AIModel):
    slug: str = D("lowercase-with-dashes, 2-40 characters.")
    label: Short[40] = D("Short label.")
    description: Short[200] = D("What the reel SHOWS or teaches, e.g. 'reels that show ad creative being made'.")
    examples: Few[Short[80], 5] = D("Example reel subjects.")

    @field_validator("slug", mode="before")
    @classmethod
    def _slug(cls, v):
        return slugify(v) if isinstance(v, str) else v


class TopicList(AIModel):
    topics: Few[TopicItem, 10] = D("Suggested topics.")


class TopicRetag(AIModel):
    reel_id: str = D("Copy the reel id exactly.")
    topics: Few[str, 3] = D("0-3 active topic slugs; [] = Unsorted.")
    new_topic: Short[40] = D("Empty, or one suggested new topic label.")


class TopicRetagBatch(AIModel):
    tags: list[TopicRetag] = D("Exactly one entry per reel id in the task.")


def slugify(text: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:40].strip("-")
    return s if len(s) >= 2 else (s + "-x" if s else "topic")


# ============================================================================================== 8 shared parts


class RefClip(AIModel):
    reel_id: str = D("Must be one of the output's source reels.")
    url: str = D("Leave empty: ReelVault fills the Instagram link.")
    start: Short[10] = D("e.g. '0:02'.")
    end: Short[10] = D("e.g. '0:05'.")
    watch_for: Short[120] = D("e.g. 'the riser into the screenshot'.")


class ScriptBeat(AIModel):
    t_start: float = D("Seconds.")
    t_end: float = D("Seconds.")
    say: Short[400] = D("Spoken words.")
    on_screen_text: Short[120] = D("Text on screen.")
    visual: Short[200] = D("What we see / the shot.")


class CueSheetRow(AIModel):
    t: Short[12] = D("e.g. '00:02.8'.")
    until: Short[12] = D("'' or e.g. '00:04.0'.")
    kind: CueKind = D("Cue kind.")
    what: Short[100] = D("e.g. 'soft whoosh into the screenshot'.")
    purpose: Short[100] = D("e.g. 'marks the jump to proof'.")
    level: Literal["subtle", "normal", "loud"] = D("How loud.")
    pattern: SDId = D("Which sound pattern this cue belongs to.")
    source_hint: Short[120] = D("Where to get it: search terms for CapCut sounds, Pixabay or free SFX libraries.")
    ref: RefClip | None = D("The source reel moment that has this cue, or null.")


class MusicBrief(AIModel):
    type: Literal["original_bed", "licensed_trending", "none"] = D("Music type.")
    mood: Short[80] = D("Mood.")
    bpm_range: Short[20] = D("e.g. '90-110'.")
    level: Short[120] = D("e.g. 'about -24 dB under the voice; duck 6 dB in gaps'.")
    options: Few[Short[120], 5] = D("0-5 track titles from 'Music the outliers used'.")
    notes: Short[300] = D("Notes.")


class EditorBrief(AIModel):
    format: VFId = D("Visual format id.")
    format_recipe: Short[500] = D("How to shoot and edit the format.")
    aspect: Literal["9:16"] = D("Always 9:16.")
    length_s: int = D("Target length in seconds.")
    pace: Short[160] = D("e.g. 'a cut every 1.5-2 s; hook cuts faster'.")
    caption_style: CaptionStyle = D("Caption style.")
    caption_look: Short[120] = D("Font look, case, color, position.")
    broll: Few[Short[160], 10] = D("Shots to find or film.")
    graphics: Few[Short[120], 8] = D("Graphics and stickers.")
    proof_inserts: Few[Short[160], 6] = D("Proof screenshots to show.")
    color_look: Short[120] = D("Color and look.")
    music: MusicBrief = D("Music brief.")
    references: Few[RefClip, 5] = D("2-5 moments to watch in the source reels.")
    dos: Few[Short[160], 6] = D("Do this.")
    donts: Few[Short[160], 6] = D("Don't do this.")


class TooClose(AIModel):
    line: Short[300] = D("A line still too close to the original.")
    why: Short[200] = D("What it copies and how to change it.")


# ============================================================================================== 8.1 reel scripts


class Upgrade(AIModel):
    vs_source: str = D("The source reel id this beats.")
    fixes: str = D("'<reel_id>:W2' (a weakness id of that reel) or 'card:<dimension>:<value>' (a working card).")
    change: Short[200] = D("What we do differently.")
    why: Short[160] = D("Why it is better.")


class ReelScript(AIModel):
    title: Short[100] = D("Script title.")
    based_on: Few[str, 5] = D("Source reel ids it builds on.")
    angle: Short[300] = D("What makes this better and more personal than the originals.")
    format: VFId = D("Visual format id (keep the source's format).")
    sub_format: str = D("Format id (fmt_...) when built on a named sub-format, else ''.")
    topic: str = D("Topic slug or 'unsorted'.")
    pain: str = D("One of the member's customer pains, copied exactly.")
    proof_used: str = D("One item of the member's proof, copied exactly, or 'none available'.")
    upgrades: Few[Upgrade, 5] = D("3-5 concrete ways this beats the source.")
    length_s: int = D("Length in seconds.")
    hook: Hook = D("The hook.")
    beats: Few[ScriptBeat, 20] = D("Beats in order; times add up to length_s.")
    cta: CTA = D("Call to action.")
    caption: Short[2200] = D("Post caption.")
    hashtags: Few[Short[40], 10] = D("Hashtags.")
    editor_brief: EditorBrief = D("Hand-off brief for the editor.")
    sound_cue_sheet: Few[CueSheetRow, 20] = D("4-20 rows; times match the beats.")
    why_it_should_work: Few[Short[200], 5] = D(
        "Each ties to evidence, e.g. 'VF04 is 3.1x more likely to be an outlier here'."
    )
    too_close: Few[TooClose, 5] = D("Lines still too close to the original. [] if none.")


class ReelScriptPack(AIModel):
    kind: Literal["reel_script"] = D("Always 'reel_script'.")
    scripts: Few[ReelScript, 5] = D("The scripts asked for in this call.")


# ============================================================================================== 8.2 Meta ads


class AdScript(AIModel):
    length_s: Literal[15, 30] = D("15 or 30.")
    beats: Few[ScriptBeat, 12] = D("Beats in order.")
    cta: Short[200] = D("Call to action.")
    editor_notes: Short[600] = D("Notes for the editor.")
    sound_cues: Few[CueSheetRow, 12] = D("Sound cues.")


class TestHook(AIModel):
    id: Literal["H1", "H2", "H3", "H4", "H5"] = D("H1 to H5.")
    angle: Literal["pain", "result", "contrarian", "curiosity", "social_proof", "offer", "callout"] = D(
        "Angle; all 5 differ."
    )
    spoken: Short[200] = D("Spoken hook.")
    on_screen_text: Short[100] = D("Text on screen.")
    visual: Short[200] = D("What we see.")
    first_frame: Short[200] = D("The first frame.")


class HookTest(AIModel):
    body: Literal["ad_30"] = D("The 5 hooks swap the first 3 s of the 30 s ad.")
    hooks: list[TestHook] = D("Exactly 5 hooks with 5 different angles.")
    setup: Few[Short[200], 8] = D("How to run it in Meta Ads Manager, plain steps.")
    read_results: Few[Short[200], 6] = D(
        "What to look at (3-second view rate, hold rate, cost per lead) and when to stop."
    )

    @model_validator(mode="after")
    def _five(self):
        if len(self.hooks) != 5:
            raise ValueError(f"hook_test.hooks must have exactly 5 hooks, got {len(self.hooks)}")
        if len({h.angle for h in self.hooks}) != 5:
            raise ValueError("hook_test.hooks must use 5 different angles")
        for i, h in enumerate(self.hooks):
            h.id = f"H{i + 1}"
        return self


class MetaAd(AIModel):
    angle: Short[200] = D("The ad's angle.")
    awareness: Literal["unaware", "problem_aware", "solution_aware", "product_aware", "most_aware"] = D(
        "Audience awareness."
    )
    offer: Short[200] = D("What the ad sells.")
    ad_15: AdScript = D("The 15 s script.")
    ad_30: AdScript = D("The 30 s script.")
    hook_test: HookTest = D("The 5-hook test.")
    primary_text: Short[600] = D("First line 125 characters or less.")
    headline: Short[40] = D("Headline.")
    description: Short[30] = D("Description.")
    cta_button: Literal[
        "LEARN_MORE",
        "SIGN_UP",
        "BOOK_NOW",
        "GET_QUOTE",
        "CONTACT_US",
        "APPLY_NOW",
        "SEND_MESSAGE",
        "DOWNLOAD",
        "GET_OFFER",
        "SUBSCRIBE",
    ] = D("Meta CTA button.")
    based_on: Few[str, 5] = D("Source reel ids.")
    policy_notes: Few[Short[200], 6] = D(
        "Meta ad policy risks found (personal attributes, income claims, before/after)."
    )

    @model_validator(mode="after")
    def _lengths(self):
        self.ad_15.length_s, self.ad_30.length_s = 15, 30
        return self


class MetaAdPack(AIModel):
    kind: Literal["meta_ads"] = D("Always 'meta_ads'.")
    ads: Few[MetaAd, 3] = D("The ad sets asked for in this call.")


# ============================================================================================== 8.3 content plan


class Pillar(AIModel):
    topic: str = D("Topic slug or 'unsorted'.")
    label: Short[40] = D("Label.")
    share_pct: int = D("Share of posts; all pillars add up to 100.")
    why: Short[200] = D("Why this pillar.")


class PlanDay(AIModel):
    date: str = D("YYYY-MM-DD.")
    post_type: Literal["reel", "carousel", "story", "live", "rest"] = D(
        "Only reel or rest unless other types are allowed."
    )
    topic: str = D("Topic slug or 'unsorted'.")
    format: VFOrNone = D("Visual format id, or 'none' on rest days.")
    builds_on: str = D("Required for reels: 'fmt:<id>' (a format card) or 'reel:<id>' (an outlier). '' on rest days.")
    hook: Short[200] = D("The hook.")
    idea: Short[300] = D("The idea.")
    cta_type: CTAType = D("CTA type.")
    based_on: Few[str, 3] = D("Reel ids.")
    effort: Literal["quick", "medium", "deep"] = D("Effort.")


class StoredWeek(AIModel):
    """A week as stored in outputs.data (the last one is cut at day 30, so it may hold fewer than 7 days)."""

    week: int = D("Week number, 1-5.")
    theme: Short[100] = D("Theme.")
    days: list[PlanDay] = D("Exactly 7 days.")


class PlanWeek(StoredWeek):
    @model_validator(mode="after")
    def _seven(self):
        if len(self.days) != 7:
            raise ValueError(f"week {self.week} must have exactly 7 days, got {len(self.days)}")
        return self


class ContentPlan30(AIModel):
    kind: Literal["content_plan"] = D("Always 'content_plan'.")
    start_date: str = D("YYYY-MM-DD.")
    posts_per_week: int = D("Posts per week.")
    pillars: Few[Pillar, 5] = D("3-5 pillars; share_pct adds up to 100.")
    weeks: Few[StoredWeek, 5] = D("5 weeks cover 30 days (days past day 30 are dropped).")
    batching_tips: Few[Short[200], 6] = D("Batching tips.")
    measure: Few[Short[200], 6] = D("What to measure.")


class ContentPlanChunk(AIModel):
    kind: Literal["content_plan"] = D("Always 'content_plan'.")
    chunk_index: int = D("Copy the chunk index from the task.")
    pillars: Few[Pillar, 5] = D("Chunk 0: 3-5 pillars adding up to 100. Other chunks: [].")
    weeks: Few[PlanWeek, 1] = D("Exactly the one week this chunk asks for.")
    batching_tips: Few[Short[200], 6] = D("Chunk 0 only, else [].")
    measure: Few[Short[200], 6] = D("Chunk 0 only, else [].")


# ============================================================================================== 8.4 competitor report


class CompetitorRead(AIModel):
    handle: str = D("Competitor handle without @, from the numbers.")
    what_they_do: Short[300] = D("Cite card values like 'VF04' or reel ids, never new numbers.")
    what_works: Short[300] = D("Cite card values or reel ids, never new numbers.")
    best_hooks: Few[Short[160], 3] = D("Best hooks.")
    cta_pattern: Short[160] = D("CTA pattern.")
    sound_pattern: Short[160] = D("Sound pattern.")
    gaps: Few[Short[160], 3] = D("What they don't do.")


class Finding(AIModel):
    finding: Short[240] = D("The finding in plain words.")
    card_value: str = D("'<dimension>:<value>' that exists in the numbers, e.g. 'visual_format:VF04'.")
    how_to_use: Short[240] = D("How the member uses it.")


class RemakeItem(AIModel):
    reel_id: str = D("Must be one of numbers.remake_candidates.")
    why: Short[200] = D("Why remake it.")
    angle: Short[200] = D("The member's version in one line.")


class RecFormat(AIModel):
    format: VFId = D("Visual format id the member can make.")
    sub_format: str = D("fmt id or ''.")
    why: Short[240] = D("Why. Start with 'If you can ...' for a format the member can't make yet.")
    first_script_idea: Short[240] = D("A first script idea.")


class CustomerGap(AIModel):
    pain: str = D("One of the member's customer pains, copied exactly.")
    gap: Short[200] = D("e.g. 'No competitor shows how to fix X'.")
    idea: Short[200] = D("A reel idea that fills it.")


class PainCoverage(AIModel):
    pain: str = D("One of the member's customer pains, copied exactly.")
    covered: bool = D("True when competitor reels already cover it.")


class CompetitorReport(AIModel):
    kind: Literal["competitor_report"] = D("Always 'competitor_report'.")
    headline: Few[Short[160], 3] = D("3 lines.")
    competitors: Few[CompetitorRead, 10] = D("One per competitor in the numbers.")
    working: Few[Finding, 6] = D("What works.")
    overused: Few[Finding, 4] = D("What is overused.")
    opportunities: Few[Finding, 4] = D("Opportunities.")
    sound_design: Few[Short[200], 5] = D("What their sound does and what to copy.")
    recommended_formats: Few[RecFormat, 4] = D("2-4 formats the member can make.")
    remake_queue: Few[RemakeItem, 10] = D("5-10 reels to remake first, in order.")
    customer_gaps: Few[CustomerGap, 5] = D("0-5 member pains no competitor covers.")
    pain_coverage: Few[PainCoverage, 5] = D("For each customer pain: covered or not covered. Nothing else.")
    next_steps: Few[Short[200], 5] = D("Up to 5 next steps.")


OutputData = Annotated[
    Union[ReelScriptPack, MetaAdPack, ContentPlanChunk, CompetitorReport], Field(discriminator="kind")
]

# ============================================================================================== registry

MODELS: dict[str, type[BaseModel]] = {
    m.__name__: m
    for m in (
        QuickTagBatch,
        ReelBreakdown,
        FormatGuideBatch,
        ProfileDraft,
        TopicList,
        TopicRetagBatch,
        ReelScriptPack,
        MetaAdPack,
        ContentPlanChunk,
        ContentPlan30,
        CompetitorReport,
    )
}

# Output kind -> the model of ONE chunk (the API/MCP/desktop answer for one call).
CHUNK_MODELS: dict[str, type[BaseModel]] = {
    "reel_script": ReelScriptPack,
    "meta_ads": MetaAdPack,
    "content_plan": ContentPlanChunk,
    "competitor_report": CompetitorReport,
}
# Output kind -> the merged model stored in outputs.data.
OUTPUT_MODELS: dict[str, type[BaseModel]] = {
    "reel_script": ReelScriptPack,
    "meta_ads": MetaAdPack,
    "content_plan": ContentPlan30,
    "competitor_report": CompetitorReport,
}


def model_for(schema_name: str) -> type[BaseModel]:
    try:
        return MODELS[schema_name]
    except KeyError:
        raise KeyError(f"unknown schema {schema_name!r}") from None


# ============================================================================================== field guide


def _limits(meta) -> list[str]:
    return [m.text() for m in meta if isinstance(m, Limit) and m.unit in ("chars", "items")]


def _unwrap(tp):
    """(inner type, [limit texts]) for Annotated / list / Optional types."""
    notes: list[str] = []
    while True:
        origin = get_origin(tp)
        if origin is Annotated:
            args = get_args(tp)
            notes += _limits(args[1:])
            tp = args[0]
            continue
        return tp, notes


def field_guide(model_cls: type[BaseModel], prefix: str = "") -> str:
    """'- path: description (≤ 120 chars)' lines for every field, recursively. This is where text limits and
    counts live (never in the schema)."""
    lines: list[str] = []
    seen: set[type] = set()

    def walk(cls: type[BaseModel], path: str) -> None:
        if cls in seen and path:
            pass
        for name, f in cls.model_fields.items():
            p = f"{path}.{name}" if path else name
            notes = _limits(f.metadata)
            tp, more = _unwrap(f.annotation)
            notes += more
            item_notes: list[str] = []
            inner = tp
            if get_origin(tp) in (list, tuple):
                args = get_args(tp)
                inner, item_notes = _unwrap(args[0]) if args else (None, [])
                p_items = p + "[]"
            else:
                p_items = p
            if get_origin(inner) is Union or str(get_origin(inner)) == "types.UnionType":
                subs = [a for a in get_args(inner) if a is not type(None)]
                inner = subs[0] if len(subs) == 1 else inner
            desc = f.description or ""
            extra = notes + [f"each {t}" for t in item_notes]
            if desc or extra:
                lines.append(f"- {p}: {desc}" + (f" ({'; '.join(extra)})" if extra else ""))
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                walk(inner, p_items)

    walk(model_cls, prefix)
    return "\n".join(lines)
