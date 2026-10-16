#!/usr/bin/env python3
"""AnomalyGuessr "Needs work" repair pass (ticket #2590).

The moderation view's third button (frontend.tsx) stores a repair request in
``feedback.json`` (``repairs[id]`` with ``status: "requested"`` and the ticked
reasons) and takes the scene out of the moderation queue. This pass is the
other half: the project heartbeat runs it, it resolves each pending request
and either puts the scene back into moderation (``repaired``) or returns it
to rejected with the reason that made it unrepairable (``unrepairable``).

What it may repair, and how:

- **Click area** (``click area doesn't cover anomaly`` / ``click area too
  big``): the answer is re-localized with the ``ag_verify`` vision pass on the
  stored edited image (the same pass generation uses, #1165) and confirmed
  with a crop contact-sheet: the anomaly must be visible in the crop around
  the proposed center in the EDITED image and absent in the same crop of the
  ORIGINAL. Only a confirmed box is written back (into ``state.json``, with
  the old circle kept as ``answer_before``). The source of truth is that
  vision pass plus the crop test, not the old automatic click-target pass
  (#1445), which corrected 0 of 5 answers and is not repeated.
- **Text** (``text is inaccurate``): the explanation (why plus reference) is
  regenerated against the stored image and then checked against the image
  before it is written back.

What it never repairs: ``too easy``, ``too hard``, ``doesn't match style of
image``, ``anomaly doesn't make sense in context of image`` and ``scaling of
anomaly is wrong`` need a different image or a different scene, so a request
carrying any of them comes back rejected, named in the report. A repair whose
check does not confirm the change is unrepairable too; a tooling/network
error leaves the request pending for the next run. Nothing is dropped
silently.

Usage::

    ag_repair.py --data DIR [--env .env] [--dry-run] [--summary]

Writer note: ``feedback.json`` is normally the API's file (api/), and
``state.json`` is ``ag_queue.py``'s. This pass has to update both (the
verdicts and the scene's answer/text), so it does a fresh read-modify-write
with an atomic rename on each, immediately before writing. It runs at 06:00
in the heartbeat, hours away from live moderation, so the lost-update window
is tiny; the wiki page records the exception.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ag_generate as g  # noqa: E402
import ag_llm  # noqa: E402
import ag_queue  # noqa: E402
import ag_verify  # noqa: E402

# Reasons the pass can act on. Anything else is unrepairable by an answer or
# text change and needs a new image or a new scene.
CLICK_REASONS = ("click area doesn't cover anomaly", "click area too big")
TEXT_REASONS = ("text is inaccurate",)
# The whole repairable set, derived from the two repair classes above. The
# moderation view mirrors it (repairReasons.ts) to enable "Needs work" only for
# these reasons; a test reads this tuple back, so a one-sided rename fails
# (ticket #2658).
REPAIRABLE_REASONS = CLICK_REASONS + TEXT_REASONS
# Reasons that make a request unrepairable outright, whatever else it carries.
UNREPAIRABLE_REASONS = (
    "doesn't match style of image",
    "scaling of anomaly is wrong",
    "anomaly doesn't make sense in context of image",
    "too easy",
    "too hard",
)

DEFAULT_TEXT_MODEL = g.MODEL


def is_person(scene: dict) -> bool:
    """Whether the anomaly is a human figure (ag_verify's person rule, #1308).

    The queue entry stores only the label, not the proposal's figure flag, so
    the label is the tell: the generator names time-traveller people "Time
    traveler: ..." and "Robot time traveler".
    """
    label = str((scene or {}).get("anomaly") or "")
    return label.startswith("Time traveler") or label.startswith("Robot")


def classify(reasons) -> str:
    """The repair kind for a request: click, text, click+text or unrepairable.

    One unrepairable reason makes the whole request unrepairable: fixing the
    click area of a scene Evan finds too easy still leaves it too easy.
    """
    rs = {str(r).strip() for r in (reasons or []) if str(r).strip()}
    if rs & set(UNREPAIRABLE_REASONS):
        return "unrepairable"
    parts = []
    if rs & set(CLICK_REASONS):
        parts.append("click")
    if rs & set(TEXT_REASONS):
        parts.append("text")
    return "+".join(parts) if parts else "unrepairable"


def unrepairable_reasons(reasons) -> list:
    """The reasons that block a repair, in canonical order (unknowns last)."""
    rs = [str(r).strip() for r in (reasons or []) if str(r).strip()]
    known = [r for r in UNREPAIRABLE_REASONS if r in rs]
    unknown = [r for r in rs if r not in UNREPAIRABLE_REASONS
               and r not in REPAIRABLE_REASONS]
    return known + unknown


def pending_requests(feedback: dict) -> list:
    """[(scene_id, reasons)] for every open repair request, sorted by id."""
    repairs = feedback.get("repairs") or {}
    out = []
    for sid, entry in repairs.items():
        if not isinstance(entry, dict) or entry.get("status") != "requested":
            continue
        reasons = [str(r) for r in (entry.get("reasons") or [])]
        out.append((str(sid), reasons))
    out.sort()
    return out


def library_paths(data_dir: Path, scene_id: str):
    """(edited, original) image paths of a queue scene."""
    base = Path(data_dir) / "library" / scene_id
    return base / f"{scene_id}.jpg", base / f"{scene_id}-original.jpg"


# ── The two repairs ────────────────────────────────────────────────────────

def _parse_json_answer(content: str) -> dict:
    """The JSON object in a model answer, {} when there is none."""
    import re
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content or "").strip())
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def _vision_ask(image: Path, prompt: str, api_key: str, model: str) -> str:
    """One raw vision call on an image with a custom prompt; returns text."""
    import base64
    b64 = base64.b64encode(Path(image).read_bytes()).decode()
    payload = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url",
         "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]}]}
    body = ag_verify._vision_post(payload, api_key)
    return ag_verify._message_content(body, model)


def _montage(left: Path, right: Path, dest: Path) -> None:
    """Append two crops side by side into one contact sheet (ImageMagick)."""
    import subprocess
    subprocess.run(["convert", str(left), str(right), "+append", str(dest)],
                   check=True, capture_output=True)


def contact_sheet_prompt(anomaly: str) -> str:
    return (
        "Two crops of the same region of a historical photograph, side by "
        "side. The LEFT crop comes from the edited photo, the RIGHT crop from "
        "the untouched original. Exactly one anomaly was edited into the "
        "original photo: " + anomaly + ". In which crop is that anomaly "
        "visible? Answer as strict JSON only, no prose: "
        '{"present_in": "left"|"right"|"both"|"neither", "note": "<one short '
        'sentence>"}'
    )


def confirm_crop(edited: Path, original: Path, answer: dict, anomaly: str,
                 api_key: str, model: str, ask=None, montage=None) -> dict:
    """The crop contact-sheet test for a proposed click-area center (#2590).

    One contact sheet shows the crop around the proposed center in the EDITED
    image (left) and the same crop of the ORIGINAL (right); the vision model
    must say the anomaly is visible in the LEFT crop only. That is the manual
    #1285 recipe, made routine: it rejects a re-localization that landed on a
    JPEG re-encode artifact (visible in both) or on empty background (visible
    in neither), and a single side-by-side call is sturdier than two separate
    presence questions.
    """
    ask = ask or _vision_ask
    window, _ = ag_verify.crop_window(
        Path(edited), {"cx": answer["x"], "cy": answer["y"]})
    with tempfile.TemporaryDirectory() as td:
        ec = Path(td) / "edited-crop.png"
        oc = Path(td) / "original-crop.png"
        sheet = Path(td) / "contact.png"
        ag_verify.save_crop(Path(edited), window, ec)
        ag_verify.save_crop(Path(original), window, oc)
        (montage or _montage)(ec, oc, sheet)
        content = ask(sheet, contact_sheet_prompt(anomaly), api_key, model)
    parsed = _parse_json_answer(content)
    where = str(parsed.get("present_in") or "").strip().lower()
    note = str(parsed.get("note") or "")[:200]
    if where == "left":
        return {"ok": True, "note": note}
    if where == "both":
        return {"ok": False,
                "reason": "the anomaly is visible in both crops, so the "
                          "proposed center is likely a re-encode artifact, "
                          "not the edit"}
    if where == "right":
        return {"ok": False,
                "reason": "the anomaly is visible only in the original crop, "
                          "not at the proposed center in the edited one"}
    if where == "neither":
        return {"ok": False,
                "reason": "the anomaly is not visible in the crop around the "
                          "proposed center"}
    return {"ok": False,
            "reason": "the contact-sheet check gave no usable answer: "
                      f"{note or str(content)[:120]}"}


def repair_click(scene: dict, data_dir: Path, api_key: str, model: str,
                 verify=None, confirm=None) -> dict:
    """Re-localize the answer; only a crop-confirmed vision box is returned."""
    verify = verify or ag_verify.verify
    confirm = confirm or confirm_crop
    edited, original = library_paths(data_dir, scene["id"])
    if not edited.exists() or not original.exists():
        return {"ok": False,
                "reason": f"missing stored image for {scene['id']}"}
    verdict = verify(edited, original, scene["anomaly"], vision=True,
                     person=is_person(scene), diff=True)
    if not verdict.get("ok"):
        return {"ok": False,
                "reason": str(verdict.get("reason") or "verification failed")}
    if verdict.get("localization") != "vision" or not verdict.get("answer"):
        return {"ok": False,
                "reason": "the vision pass returned no usable bounding box"}
    answer = verdict["answer"]
    check = confirm(edited, original, answer, scene["anomaly"], api_key, model)
    if not check["ok"]:
        return {"ok": False, "reason": check["reason"]}
    box = verdict.get("box") or (verdict.get("vision") or {}).get("box")
    return {
        "ok": True,
        "answer": {"x": float(answer["x"]), "y": float(answer["y"]),
                   "r": float(answer["r"])},
        "box": box,
        "detail": f"re-localized on the vision box {box}; "
                  f"crop contact-sheet confirmed",
    }


def text_repair_prompt(scene: dict) -> str:
    return (
        "This is an edited historical photo. Exactly one anomaly was edited "
        "into the original: " + str(scene.get("anomaly") or "") + ". The "
        "scene's year is " + str(scene.get("year") or "") + ". The stored "
        "explanation was rejected as inaccurate: "
        + str(scene.get("explanation") or "") + ". Write a corrected one-"
        "sentence explanation of why this anomaly could not exist in that "
        "year, true of the photo. Answer as strict JSON only, no prose: "
        '{"explanation": "<one sentence>", "references": [{"label": '
        '"<source name>", "url": "https://..."}], "matches_image": true|false, '
        '"reason": "<one short sentence>"}. matches_image is true only when '
        "the anomaly is clearly visible in THIS photo and the explanation "
        "describes it."
    )


def text_check_prompt(scene: dict, explanation: str) -> str:
    return (
        "This is an edited historical photo. Exactly one anomaly was edited "
        "into the original: " + str(scene.get("anomaly") or "") + ". A "
        "proposed explanation of why it could not exist in "
        + str(scene.get("year") or "") + " reads: " + explanation + ". Does "
        "the photo show this anomaly, and does the explanation describe what "
        "the photo actually shows? Answer as strict JSON only, no prose: "
        '{"matches": true|false, "reason": "<one short sentence>"}'
    )


def parse_references(raw) -> list:
    """Up to three well-formed {label, url} references from a model answer."""
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()[:120]
        url = str(item.get("url") or "").strip()
        if label and url.startswith(("http://", "https://")):
            out.append({"label": label, "url": url})
        if len(out) >= 3:
            break
    return out


def repair_text(scene: dict, data_dir: Path, api_key: str, model: str,
                chat=None) -> dict:
    """Regenerate the explanation, check it against the image, then return it."""
    chat = chat or ag_llm.chat
    edited, _ = library_paths(data_dir, scene["id"])
    if not edited.exists():
        return {"ok": False, "reason": f"missing stored image for {scene['id']}"}
    messages = [{"role": "user", "content": [
        ag_llm.text_part(text_repair_prompt(scene)),
        ag_llm.image_part(edited),
    ]}]
    body = chat(messages, api_key, model, max_tokens=600, reasoning=False)
    parsed = ag_llm.parse_json(ag_llm.content_of(body)) or {}
    explanation = ag_llm.clean_text(parsed.get("explanation"), 400)
    if not explanation:
        return {"ok": False, "reason": "the model returned no explanation"}
    references = parse_references(parsed.get("references")) \
        or list(scene.get("references") or [])
    # A second, independent call checks the new text against the image; a
    # self-report is not a check.
    check_messages = [{"role": "user", "content": [
        ag_llm.text_part(text_check_prompt(scene, explanation)),
        ag_llm.image_part(edited),
    ]}]
    check_body = chat(check_messages, api_key, model, max_tokens=400,
                      reasoning=False)
    check = ag_llm.parse_json(ag_llm.content_of(check_body)) or {}
    if not check.get("matches"):
        return {"ok": False,
                "reason": "the regenerated text did not pass the image "
                          f"check: {str(check.get('reason') or '')[:200]}"}
    return {"ok": True, "explanation": explanation, "references": references,
            "detail": str(check.get("reason") or "")[:200]}


# ── Resolving one request ──────────────────────────────────────────────────

def repair_one(scene_id: str, scene: dict, kind: str, reasons: list,
               data_dir: Path, api_key: str, vision_model: str,
               text_model: str, *, verify=None, confirm=None,
               chat=None) -> dict:
    """The outcome of one request: repaired, unrepairable or failed.

    A tooling/network error (``RuntimeError``/``ag_llm.LLMError``) is a
    failure that leaves the request pending; a repair that ran but did not
    pass its check is unrepairable, named.
    """
    result = {"id": scene_id, "reasons": reasons, "kind": kind}
    if kind == "unrepairable":
        blocking = unrepairable_reasons(reasons)
        return {**result, "status": "unrepairable",
                "detail": "not fixable by a repair: " + ", ".join(blocking)}
    if not api_key:
        return {**result, "status": "failed",
                "detail": "no OPENROUTER_API_KEY (pass --env)"}
    answer_after = text_after = None
    details = []
    try:
        if "click" in kind:
            click = repair_click(scene, data_dir, api_key, vision_model,
                                 verify=verify, confirm=confirm)
            if not click["ok"]:
                return {**result, "status": "unrepairable",
                        "detail": click["reason"]}
            answer_after = click["answer"]
            details.append(click["detail"])
        if "text" in kind:
            text = repair_text(scene, data_dir, api_key, text_model, chat=chat)
            if not text["ok"]:
                return {**result, "status": "unrepairable",
                        "detail": text["reason"]}
            text_after = text["explanation"]
            details.append("text regenerated and checked against the image")
    except (RuntimeError, ag_llm.LLMError, OSError) as e:
        return {**result, "status": "failed",
                "detail": f"tooling error, will retry: {e}"}
    out = {**result, "status": "repaired", "detail": "; ".join(details)}
    if answer_after is not None:
        out["answerBefore"] = dict(scene.get("answer") or {})
        out["answerAfter"] = answer_after
    if text_after is not None:
        out["textBefore"] = str(scene.get("explanation") or "")
        out["textAfter"] = text_after
        out["references"] = text["references"]
    return out


# ── Applying the outcomes ──────────────────────────────────────────────────

def save_feedback(data_dir: Path, feedback: dict) -> None:
    """Atomic feedback.json write (mirrors the API's tmp + rename)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(data_dir), prefix=".feedback-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(feedback, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, data_dir / "feedback.json")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def apply_results(data_dir: Path, results: list, now: str) -> dict:
    """Write the outcomes into state.json + feedback.json (fresh re-reads)."""
    state = ag_queue.load_state(Path(data_dir))
    feedback = ag_queue.load_feedback(Path(data_dir))
    state_dirty = False
    scenes = state.get("scenes") or {}
    repairs = feedback.setdefault("repairs", {})
    for r in results:
        sid = r["id"]
        if r["status"] == "failed":
            continue  # stays requested; retried on the next run
        entry = repairs.get(sid) or {}
        record = {
            "status": r["status"],
            "reasons": list(entry.get("reasons") or r.get("reasons") or []),
            "requestedAt": entry.get("requestedAt") or now,
            "resolvedAt": now,
            "kind": r.get("kind") or "none",
            "detail": r.get("detail") or "",
        }
        scene = scenes.get(sid)
        if r["status"] == "repaired" and scene is not None:
            if r.get("answerAfter"):
                scene["answer_before"] = dict(scene.get("answer") or {})
                scene["answer"] = r["answerAfter"]
                state_dirty = True
            if r.get("textAfter"):
                scene["explanation"] = r["textAfter"]
                if r.get("references"):
                    scene["references"] = r["references"]
                state_dirty = True
            if r.get("answerBefore") is not None:
                record["answerBefore"] = r["answerBefore"]
            if r.get("answerAfter") is not None:
                record["answerAfter"] = r["answerAfter"]
            if r.get("textBefore") is not None:
                record["textBefore"] = r["textBefore"]
            if r.get("textAfter") is not None:
                record["textAfter"] = r["textAfter"]
            # A repaired scene is unmoderated again: it returns to the queue.
            feedback.get("accepted", {}).pop(sid, None)
            feedback.get("rejected", {}).pop(sid, None)
            feedback.get("reasons", {}).pop(sid, None)
        elif r["status"] == "unrepairable":
            feedback.setdefault("rejected", {})[sid] = now
            feedback.get("accepted", {}).pop(sid, None)
            # The request reasons become the rejection's reasons, so the
            # nightly feedback pass counts what Evan actually saw.
            record_reasons = [x for x in record["reasons"]]
            if record_reasons:
                feedback.setdefault("reasons", {})[sid] = {
                    "reason": record_reasons, "at": now}
        repairs[sid] = record
    if state_dirty:
        ag_queue.save_state(Path(data_dir), state)
    save_feedback(Path(data_dir), feedback)
    return {"stateChanged": state_dirty}


# ── The run ────────────────────────────────────────────────────────────────

def run(data_dir, *, env_path=None, dry_run: bool = False,
        vision_model: str = None, text_model: str = None,
        verify=None, confirm=None, chat=None) -> dict:
    """Resolve every pending request; returns the report dict."""
    data_dir = Path(data_dir)
    feedback = ag_queue.load_feedback(data_dir)
    state = ag_queue.load_state(data_dir)
    requests = pending_requests(feedback)
    report = {"requests": len(requests), "repaired": 0, "unrepairable": 0,
              "failed": 0, "dryRun": bool(dry_run), "results": [],
              "repairedByKind": {}, "unrepairableByReason": {}}
    if not requests:
        return report
    if env_path:
        ag_verify.load_env(Path(env_path))
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    vision_model = vision_model or ag_verify.resolve_vision_model()
    text_model = text_model or DEFAULT_TEXT_MODEL
    scenes = state.get("scenes") or {}
    results = []
    for sid, reasons in requests:
        scene = scenes.get(sid)
        if scene is None:
            results.append({"id": sid, "reasons": reasons,
                            "kind": classify(reasons), "status": "failed",
                            "detail": "scene not found in state.json"})
            continue
        kind = classify(reasons)
        if dry_run:
            results.append({"id": sid, "reasons": reasons, "kind": kind,
                            "status": "planned", "detail": "dry run"})
            continue
        results.append(repair_one(sid, scene, kind, reasons, data_dir,
                                  api_key, vision_model, text_model,
                                  verify=verify, confirm=confirm,
                                  chat=chat))
    if not dry_run:
        apply_results(data_dir, results, _now())
    for r in results:
        status = r["status"]
        if status == "repaired":
            report["repaired"] += 1
            report["repairedByKind"][r["kind"]] = \
                report["repairedByKind"].get(r["kind"], 0) + 1
        elif status == "unrepairable":
            report["unrepairable"] += 1
            blocking = unrepairable_reasons(r["reasons"])
            if blocking:
                key = blocking[0]
            else:
                key = f"{r['kind']} check failed"
            report["unrepairableByReason"][key] = \
                report["unrepairableByReason"].get(key, 0) + 1
        elif status == "failed":
            report["failed"] += 1
        report["results"].append(r)
    return report


def _now() -> str:
    import datetime
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def digest(report: dict) -> str:
    """The compact heartbeat report: counts per class, then one line each."""
    if report["requests"] == 0:
        return "AnomalyGuessr repair: no pending requests"
    head = (f"AnomalyGuessr repair: {report['requests']} request(s) - "
            f"{report['repaired']} repaired, {report['unrepairable']} "
            f"unrepairable, {report['failed']} failed")
    lines = [head]
    if report["repairedByKind"]:
        parts = ", ".join(f"{k} {v}"
                          for k, v in sorted(report["repairedByKind"].items()))
        lines.append(f"  repaired by class: {parts}")
    if report["unrepairableByReason"]:
        parts = ", ".join(f"{k} {v}" for k, v
                          in sorted(report["unrepairableByReason"].items()))
        lines.append(f"  unrepairable by reason: {parts}")
    for r in report["results"]:
        if r["status"] == "planned":
            continue
        lines.append(f"  - {r['id']} {r['status']} ({r['kind']}): "
                     f"{r.get('detail', '')}")
    return "\n".join(lines)


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="ag_repair.py")
    p.add_argument("--data", required=True, help="queue data dir")
    p.add_argument("--env", default=None, help=".env path for API keys")
    p.add_argument("--vision-model", default=None)
    p.add_argument("--text-model", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be repaired, write nothing")
    p.add_argument("--summary", action="store_true",
                   help="print only the compact digest")
    p.add_argument("--json", action="store_true",
                   help="print the full report JSON instead of the digest")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    report = run(args.data, env_path=args.env, dry_run=args.dry_run,
                 vision_model=args.vision_model, text_model=args.text_model)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(digest(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
