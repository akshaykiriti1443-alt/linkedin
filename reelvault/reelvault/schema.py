"""The shape of one analyzed reel.

Claude fills this model through structured outputs, so every field here is
something you get back for every saved video.
"""

from __future__ import annotations

from typing import List

from pydantic import BaseModel, Field


class Hook(BaseModel):
    spoken: str = Field(description="The exact words of the hook (first 1-3 seconds). Empty if none.")
    on_screen_text: str = Field(description="Text overlay shown during the hook. Empty if none.")
    visual: str = Field(description="What the viewer sees in the first seconds (the visual hook).")
    hook_type: str = Field(
        description=(
            "One of: question, bold_claim, curiosity_gap, pain_point, stat_or_number, contrarian, "
            "story_open, mistake_callout, how_to_promise, list_promise, before_after, pattern_interrupt, other"
        )
    )
    why_it_works: str = Field(description="One or two sentences on the psychology behind the hook.")
    ends_at_seconds: float = Field(description="Roughly when the hook ends, in seconds.")


class Beat(BaseModel):
    start_seconds: float
    point: str = Field(description="What this beat says or teaches.")
    visual: str = Field(description="What is on screen during this beat.")
    purpose: str = Field(description="Role in the story: context, proof, step, example, twist, payoff, etc.")


class CTA(BaseModel):
    text: str = Field(description="The exact call to action, spoken or written. Empty if none.")
    cta_type: str = Field(
        description="One of: follow, comment_keyword, dm, link_in_bio, save, share, buy, watch_next, none"
    )
    placement: str = Field(description="Where it appears: start, middle, end, caption, or none.")
    keyword: str = Field(description="Comment or DM keyword if the CTA uses one, else empty.")


class Skeleton(BaseModel):
    structure_name: str = Field(
        description="Name of the pattern, e.g. 'Problem > Agitate > Solve', 'Listicle', 'Story > Lesson', 'Myth > Truth'."
    )
    hook: Hook
    meat: List[Beat] = Field(description="The body of the video as ordered beats.")
    cta: CTA
    pacing: str = Field(description="Notes on cut speed, length, and rhythm.")
    retention_devices: List[str] = Field(
        description="Tricks that keep people watching: open loops, countdowns, re-hooks, payoff teases, etc."
    )


class VisualAnalysis(BaseModel):
    on_screen_text: List[str] = Field(description="Every distinct text overlay, in order.")
    visual_style: str = Field(description="Look and feel: talking head, b-roll, screen recording, lighting, setting.")
    editing_techniques: List[str] = Field(description="Zooms, jump cuts, captions style, sound effects, transitions.")
    what_the_visuals_say: str = Field(
        description="The message the visuals carry by themselves, even with the sound off."
    )


class ActionStep(BaseModel):
    step: str
    why: str
    effort: str = Field(description="quick (under 15 min), medium (an hour or so), or deep (a project).")


class ScriptIdea(BaseModel):
    title: str
    hook: str
    outline: List[str]
    cta: str


class ReelAnalysis(BaseModel):
    title: str = Field(description="A short, clear title for this reel (max 10 words).")
    category: str = Field(description="Pick one category from the allowed list.")
    subcategory: str = Field(description="A narrower topic inside the category.")
    tags: List[str] = Field(description="5-10 lowercase topic tags.")
    language: str
    content_format: str = Field(
        description="One of: talking_head, voiceover_broll, text_only, screen_recording, tutorial, skit, interview, slideshow, vlog, other"
    )
    summary: str = Field(description="3-5 sentence summary of what the reel says and shows.")
    key_takeaways: List[str]
    action_plan: List[ActionStep] = Field(description="Concrete steps the viewer can take after watching.")
    skeleton: Skeleton
    visuals: VisualAnalysis
    fill_in_template: str = Field(
        description=(
            "A reusable fill-in-the-blank script built from this reel's skeleton, with [BRACKETED] slots, "
            "e.g. 'Stop doing [COMMON MISTAKE]. Do [BETTER WAY] instead...'."
        )
    )
    hook_variations: List[str] = Field(description="5 new hooks in the same style for the creator's niche.")
    script_ideas: List[ScriptIdea] = Field(description="3 new video ideas for the creator, reusing this skeleton.")
    ready_script: str = Field(
        description="One full, ready-to-shoot script for the creator's niche that reuses this skeleton. Mark [VISUAL] cues."
    )
    why_it_performed: str = Field(description="Your best read on why this reel works.")
    notable_quotes: List[str] = Field(description="Lines worth saving word for word.")
    confidence_notes: str = Field(description="Anything you could not tell, e.g. no audio or unclear frames. Empty if none.")
