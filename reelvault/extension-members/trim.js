// What the extension uploads: trimmed media (IgMedia) and users (IgUser), DESIGN-SAAS.md 5.4-5.5. Fields are copied,
// never interpreted (the server's igparse.py does that). Every id is a string. Missing fields are sent as null.
// Python twins: reelvault/igparse.py trim_media / trim_user (tests/server/test_igparse.py checks they agree).
const CAPTION_MAX = 5000;
const sid = v => (v === null || v === undefined || v === "") ? null : String(v);
const has = (o, k) => o && Object.prototype.hasOwnProperty.call(o, k);
const g = (m, k) => (has(m, k) && m[k] !== undefined) ? m[k] : null;
const toInt = v => (typeof v === "number" && Number.isFinite(v)) ? Math.trunc(v)
  : (typeof v === "string" && /^-?\d{1,20}$/.test(v) ? Number(v) : null);
const nonEmpty = o => !!o && typeof o === "object" && Object.keys(o).length > 0;

// The smallest version at least minW wide (else the widest one), as {url, width, height}.
export function smallest(versions, minW = 480) {
  const vs = (Array.isArray(versions) ? versions : []).filter(v => v && typeof v === "object" && v.url);
  if (!vs.length) return null;
  const ok = vs.filter(v => (toInt(v.width) || 0) >= minW);
  let pick;
  if (ok.length) pick = ok.reduce((a, b) => ((toInt(b.width) || 0) < (toInt(a.width) || 0) ? b : a));
  else pick = vs.reduce((a, b) => ((toInt(b.width) || 0) > (toInt(a.width) || 0) ? b : a));
  return {url: pick.url, width: toInt(pick.width), height: toInt(pick.height)};
}

export function videoOf(m) {
  let v = smallest(m.video_versions);
  if (!v) for (const c of (m.carousel_media || [])) { v = smallest(c && c.video_versions); if (v) break; }
  return v;
}

export function coverOf(m) {
  return smallest((m.image_versions2 || {}).candidates);
}

export function isReel(m) {
  return !!m && (m.product_type === "clips" || m.media_type === 2);
}

// Keep a media item when the target owns it or is a coauthor (collab); drop suggested reels from others.
export function keepForTarget(m, pk) {
  const owner = m && (m.user || m.owner);
  if (owner && String(owner.pk) === pk) return true;
  return (m && Array.isArray(m.coauthor_producers) ? m.coauthor_producers : []).some(c => c && String(c.pk) === pk);
}

export function trimMedia(m) {
  let pk = sid(m.pk);
  const idS = sid(m.id);
  let conflict = false;
  const strong = typeof m.strong_id__ === "string" ? (m.strong_id__.split("_")[0] || null) : null;
  for (const alt of [g(m, "pk_id"), strong]) {
    if (alt !== null && pk !== null && String(alt) !== pk) conflict = true;
  }
  if (idS && idS.includes("_") && pk && idS.split("_")[0] !== pk) { conflict = true; pk = idS.split("_")[0]; }
  const cap = m.caption;
  const text = (cap && typeof cap === "object") ? (cap.text ?? null) : (typeof cap === "string" ? cap : null);
  const owner = m.user || m.owner || {};
  const cm = m.clips_metadata || {};
  const mi = ((cm.music_info || {}).music_asset_info) || {};
  const osi = cm.original_sound_info || {};
  const artistPk = sid((osi.ig_artist || {}).pk);
  const ownerPk = sid(owner.pk);
  let audio = null;
  if (nonEmpty(cm)) {
    audio = {audio_type: cm.audio_type ?? null, music_title: mi.title ?? null, music_artist: mi.display_artist ?? null,
             music_canonical_id: sid(cm.music_canonical_id) ?? sid(mi.audio_cluster_id),
             original_sound_title: osi.original_audio_title ?? null,
             is_reused: nonEmpty(osi) && artistPk !== null && artistPk !== ownerPk};
  }
  return {
    pk, id: idS, code: g(m, "code"), taken_at: g(m, "taken_at"), product_type: g(m, "product_type"),
    media_type: g(m, "media_type"), pk_conflict: conflict,
    caption: text ? String(text).slice(0, CAPTION_MAX) : null,
    play_count: g(m, "play_count"), ig_play_count: g(m, "ig_play_count"), view_count: g(m, "view_count"),
    fb_play_count: g(m, "fb_play_count"), like_count: g(m, "like_count"), comment_count: g(m, "comment_count"),
    like_and_view_counts_disabled: !!g(m, "like_and_view_counts_disabled"), comments_disabled: !!g(m, "comments_disabled"),
    reshare_count: g(m, "reshare_count"), share_count: g(m, "share_count"), media_repost_count: g(m, "media_repost_count"),
    video_duration: g(m, "video_duration"), has_audio: g(m, "has_audio"),
    clips_tab_pinned_user_ids: (m.clips_tab_pinned_user_ids || []).map(String),
    timeline_pinned_user_ids: (m.timeline_pinned_user_ids || []).map(String),
    coauthor_producers: (m.coauthor_producers || []).filter(c => c && typeof c === "object")
      .map(c => ({pk: sid(c.pk), username: c.username ?? null})),
    is_paid_partnership: !!g(m, "is_paid_partnership"), audio, video: videoOf(m), cover: coverOf(m),
    owner: {pk: ownerPk, username: owner.username ?? null},
  };
}

export function trimUser(u) {
  const count = (...keys) => {
    for (const k of keys) {
      let v = u[k];
      if (v && typeof v === "object") v = v.count;
      if (v !== undefined && v !== null) return v;
    }
    return null;
  };
  return {
    pk: sid(u.pk !== undefined && u.pk !== null ? u.pk : u.id), username: u.username ?? null,
    full_name: u.full_name ?? null, biography: u.biography ?? null,
    bio_links: (u.bio_links || []).filter(b => b && typeof b === "object" && b.url).map(b => ({url: b.url, title: b.title || ""})),
    external_url: u.external_url ?? null, category: u.category_name || u.category || null,
    is_private: !!u.is_private, is_verified: !!u.is_verified,
    followers: count("follower_count", "edge_followed_by"), following: count("following_count", "edge_follow"),
    media_count: count("media_count", "edge_owner_to_timeline_media"), profile_pic_url: u.profile_pic_url ?? null,
  };
}

// The key tree of a response for /debug-sample (5.9): values replaced by their type, arrays cut to 1 element.
export function keysTree(v, depth = 0) {
  if (depth > 12) return "object";
  if (v === null || v === undefined) return "null";
  if (Array.isArray(v)) return v.length ? [keysTree(v[0], depth + 1)] : [];
  if (typeof v === "object") {
    const o = {};
    for (const [k, x] of Object.entries(v).slice(0, 300)) o[k] = keysTree(x, depth + 1);
    return o;
  }
  if (typeof v === "number") return Number.isInteger(v) ? "int" : "float";
  if (typeof v === "boolean") return "bool";
  return "str";
}
