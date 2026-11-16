#!/usr/bin/env python3
"""Compare two reviewed bundles; publish a conservative union.

No fuzzy record matching, fabricated platform IDs or changed timestamps. Thread
matching requires reciprocal unique content anchors, never participant names alone.
Empty posts remain in the private union ledger.
"""
import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import zipfile

from convert import FORMAT, RECIPE, DOM, Refusal, declared_timezone, digest, inventory
from formats import compatibility, normalized

PREFIX = "your_facebook_activity/posts/media/"


def encoded(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def key(value):
    return digest(encoded(value))


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("x") as stream:
        os.chmod(path, 0o600)
        json.dump(value, stream, ensure_ascii=True, indent=2)


class Bundle:
    def __init__(self, label, path, source, media_cache=None):
        self.label, self.path, self.source = label, Path(path), Path(source).resolve(strict=True)
        if (self.path / "INCOMPLETE").exists():
            raise Refusal("incomplete_input_bundle")
        self.manifest = json.loads((self.path / "source-manifest.json").read_text())
        self.report = json.loads((self.path / "report.json").read_text())
        if (self.source / "location/timezone.html").is_file():
            zone = declared_timezone(DOM((self.source / "location/timezone.html").read_text()).root)
            if self.report["timezone"] != str(zone):
                raise Refusal("bundle_timezone_conflicts_with_export_metadata")
        if self.report.get("format") != FORMAT:
            raise Refusal("wrong_bundle_format")
        if self.report["recipe"] != RECIPE:
            raise Refusal("reconvert_with_current_recipe")
        self.verify()
        self.media = {}
        for uri, rel in self.manifest["media_sources"].items():
            src = self.source / rel
            if src.resolve() != src or rel not in self.manifest["files"]:
                raise Refusal("unsafe_bundle_media")
            cached = (media_cache or {}).get(str(src))
            self.media[uri] = {"sha256": cached["sha256"] if cached else file_hash(src), "source": src,
                               "extension": src.suffix.lower()}
            if cached:
                self.media[uri]["cached_copy"] = cached["copy"]
        document = json.loads((self.path / "normalized.json").read_text())
        from formats import modern_files
        files = modern_files(document)
        self.profile = files["profile_information/profile_information.json"]
        self.records, self.threads = [], {}
        prov = json.loads((self.path / "provenance.json").read_text())
        self.provenance = {p["output"]: p for p in prov if p.get("status") == "parsed" and "output" in p}
        for rel, obj in sorted(files.items()):
            if rel.startswith("messages/"):
                thread = rel.rsplit("/", 1)[0]
                self.threads[thread] = {"participants": obj["participants"], "records": []}
                for i, msg in enumerate(obj["messages"]):
                    r = self.record("message", msg, f"{rel}#/messages/{i}", thread)
                    self.threads[thread]["records"].append(r)
            elif rel.startswith("posts/your_posts_"):
                for i, post in enumerate(obj):
                    self.record("post", post, f"{rel}#/{i}")
            elif rel == "posts/your_uncategorized_photos.json":
                for i, media in enumerate(obj["other_photos_v2"]):
                    self.record("media", media, f"{rel}#/other_photos_v2/{i}")

    def semantic(self, obj):
        if isinstance(obj, dict):
            return {k: ({"sha256": self.media[v]["sha256"]} if k == "uri" else self.semantic(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.semantic(v) for v in obj]
        return obj

    def record(self, kind, obj, output, thread=None):
        if output not in self.provenance:
            raise Refusal("missing_record_provenance")
        r = {"kind": kind, "object": obj, "semantic": self.semantic(obj),
             "generation": self.label, "output": output, "thread": thread,
             "provenance": self.provenance[output]}
        r["fingerprint"] = key([kind, r["semantic"]])
        self.records.append(r)
        return r

    def verify(self):
        if inventory(self.source) != self.manifest["files"]:
            raise Refusal("source_stat_changed")
        for rel, expected in self.manifest["parsed_html_sha256"].items():
            if file_hash(self.source / rel) != expected:
                raise Refusal("source_html_changed")



def match_threads(a, b):
    """Three distinct globally unique exact anchors + equal observed actors.

    A content-derived correspondence, explicitly not a recovered Facebook ID.
    Ambiguous or short histories stay separate. Record equality still compares
    every field, timestamp, actor and attachment bytes after matching context.
    """
    indices = []
    for bundle in [a, b]:
        ix = defaultdict(set)
        for t, thread in bundle.threads.items():
            for r in thread["records"]:
                # Short acknowledgments are poor context anchors.
                if len(r["object"].get("content", "").encode("utf-8")) >= 24:
                    ix[r["fingerprint"]].add(t)
        indices.append(ix)
    candidates = defaultdict(list)
    for fp in indices[0].keys() & indices[1].keys():
        left, right = indices[0][fp], indices[1][fp]
        if len(left) == len(right) == 1:
            candidates[(next(iter(left)), next(iter(right)))].append(fp)
    accepted = {}
    lefts, rights = defaultdict(set), defaultdict(set)
    for (ta, tb), fps in candidates.items():
        if len(fps) >= 3 and a.threads[ta]["participants"] == b.threads[tb]["participants"]:
            lefts[ta].add(tb)
            rights[tb].add(ta)
    for (ta, tb), fps in candidates.items():
        if lefts[ta] == {tb} and rights[tb] == {ta}:
            accepted[(ta, tb)] = sorted(fps)
    return accepted, candidates


def union_records(a, b, matches):
    contexts = {}
    for (ta, tb) in matches:
        context = key(["matched-thread", a.label, ta, b.label, tb])
        contexts[(a.label, ta)] = contexts[(b.label, tb)] = context
    buckets = defaultdict(lambda: defaultdict(list))
    # Older album HTML has titles absent from later HTML. Missing metadata is
    # not evidence of another upload. Coalesce only unique, compatible variants
    # of the same bytes AND posting timestamp; retain conflicting variants.
    media_groups = defaultdict(lambda: defaultdict(list))
    for bundle in [a, b]:
        for r in bundle.records:
            if r["kind"] in {"media", "post"}:
                if r["kind"] == "post" and not r["object"]["attachments"] and not any(
                        v.get("post", "") for v in r["object"]["data"]):
                    continue
                fields = {"title", "description"} if r["kind"] == "media" else {"title"}
                core = {k: v for k, v in r["semantic"].items() if k not in fields}
                media_groups[key([r["kind"], core])][bundle.label].append(r)
    for core, generations in media_groups.items():
        if len(generations) != 2 or any(len({r["fingerprint"] for r in rows}) != 1 for rows in generations.values()):
            continue
        left, right = generations[a.label][0], generations[b.label][0]
        fields = ["title", "description"] if left["kind"] == "media" else ["title"]
        if left["kind"] == "post" and left["object"].get("title") == right["object"].get("title"):
            continue
        if any(left["object"].get(f) and right["object"].get(f) and left["object"][f] != right["object"][f] for f in fields):
            continue
        metadata = {f: right["object"].get(f) or left["object"].get(f, "") for f in fields}
        for rows in generations.values():
            for r in rows:
                r["union_fingerprint"], r["retained_metadata"] = core, metadata
    for bundle in [a, b]:
        for r in bundle.records:
            context = contexts.get((bundle.label, r["thread"]), key([bundle.label, r["thread"]])) if r["thread"] else r["kind"]
            r["context"] = context
            buckets[(context, r.get("union_fingerprint", r["fingerprint"]))][bundle.label].append(r)
    counts = {k: Counter() for k in ["post", "message", "media"]}
    records = []
    for (context, fp), generations in sorted(buckets.items()):
        left, right = generations.get(a.label, []), generations.get(b.label, [])
        kind = (left or right)[0]["kind"]
        common = min(len(left), len(right))
        counts[kind].update({"common": common, a.label + "_only": len(left) - common,
                             b.label + "_only": len(right) - common})
        for i in range(max(len(left), len(right))):
            observations = [seq[i] for seq in [left, right] if i < len(seq)]
            native_object = deepcopy(observations[-1]["object"])
            native_object.update(observations[-1].get("retained_metadata", {}))
            records.append({"key": key([context, fp, i]), "kind": kind,
                            "context": context, "object": native_object,
                            "observations": observations, "occurrence": i})
    return records, {kind: dict(c) for kind, c in counts.items()}


def publish(a, b, out, target="normalized"):
    if target not in {"normalized", "meta-json", "timelinize"}:
        raise Refusal("unknown_target")
    if out.exists() or out.is_symlink() or any(out.resolve().is_relative_to(x.source) or x.source.is_relative_to(out.resolve()) for x in [a, b]):
        raise Refusal("unsafe_or_existing_output")
    if a.label == b.label or not all(__import__("re").fullmatch(r"[A-Za-z0-9_-]+", label) for label in [a.label, b.label]):
        raise Refusal("invalid_generation_labels")
    if a.profile != b.profile:
        raise Refusal("profile_identity_conflict")
    out.mkdir(mode=0o700)
    (out / "INCOMPLETE").touch(mode=0o600)
    matches, candidates = match_threads(a, b)
    records, comparison = union_records(a, b, matches)
    gaps = Counter()
    for r in records:
        if r["kind"] == "post" and not r["object"]["data"] and not r["object"]["attachments"]:
            r["quarantine"] = "empty_post_no_primary_payload"
            gaps[r["quarantine"]] += 1
    archive = out / "archive"
    archive.mkdir(mode=0o700)
    files = {"profile_information/profile_information.json": a.profile}
    media_map, threads, posts, photos = {}, {}, [], []
    bundles = {a.label: a, b.label: b}
    def relocate(obj, r, path=""):
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if k == "uri":
                    observation = r["observations"][-1]
                    entry = bundles[observation["generation"]].media[v]
                    uri = ("media/" if target == "normalized" else PREFIX) + r["key"] + "/" + key([path, entry["sha256"]]) + entry["extension"]
                    media_map[uri] = entry
                    result[k] = uri
                else:
                    result[k] = relocate(v, r, path + "/" + k)
            return result
        if isinstance(obj, list):
            return [relocate(v, r, path + "/" + str(i)) for i, v in enumerate(obj)]
        return obj
    for r in records:
        if "quarantine" in r:
            continue
        obj = relocate(r["object"], r)
        if r["kind"] == "post":
            posts.append(obj)
        elif r["kind"] == "media":
            photos.append(obj)
        else:
            thread = threads.setdefault(r["context"], {"participants": bundles[r["observations"][-1]["generation"]].threads[r["observations"][-1]["thread"]]["participants"], "messages": []})
            thread["messages"].append(obj)
    files["posts/your_posts_1.json"] = posts
    if photos:
        files["posts/your_uncategorized_photos.json"] = {"other_photos_v2": photos}
    for t, obj in threads.items():
        obj["messages"].sort(key=lambda m: -m["timestamp_ms"])
        files[f"messages/inbox/{t}/message_1.json"] = obj
    document = normalized(files, [])
    save(out / "normalized.json", document)
    if target == "normalized":
        save(archive / "normalized.json", document)
    else:
        for rel, obj in compatibility(files, target).items():
            save(archive / rel, obj)
    for uri, entry in media_map.items():
        dest = archive / uri
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(entry.get("cached_copy", entry["source"]), dest)
        dest.chmod(0o600)
        if file_hash(dest) != entry["sha256"]:
            raise Refusal("copied_media_mismatch")
    save(out / "media-manifest.json", {uri: {k: str(v) for k, v in entry.items()} for uri, entry in media_map.items()})
    save(out / "union-ledger.json", records)
    save(out / "thread-correspondence.json", {"accepted": [{"left": ta, "right": tb, "anchors": fps} for (ta, tb), fps in matches.items()],
                                            "candidate_pairs": len(candidates), "basis": "content correspondence, not original platform IDs; >=3 distinct unique >=24-UTF8-byte exact anchors and equal observed actor sets"})
    # Every observation keeps its generation, original source location and hash.
    # No source JSON/body is written to Git.
    for bundle in [a, b]:
        save(out / (bundle.label + "-coverage.json"), bundle.report)
        shutil.copyfile(bundle.path / "refused-records.json", out / (bundle.label + "-refused.json"))
        shutil.copyfile(bundle.path / "source-manifest.json", out / (bundle.label + "-source-manifest.json"))
    summary = {"format": FORMAT, "target": target, "comparison": comparison, "union_records": len(records),
               "thread_correspondences": len(matches), "quarantine": dict(gaps),
               "selected": {"posts": len(posts), "messages": sum(len(t["messages"]) for t in threads.values()),
                                   "standalone_media": len(photos), "media_files": len(media_map)},
               "copied_media_bytes": sum((archive / uri).stat().st_size for uri in media_map),
               "identity_conflicts": "No original record IDs available. Coincident variants retained; ambiguous context stays separate. Same-time text occurrences retained; empty posts quarantined.",
               "folds_resolved": {bundle.label: len(getattr(bundle, "fold_resolutions", [])) for bundle in [a, b]},
               "occurrence_policy": "retain same-time exact and case variants; deterministic locations require occurrence-safe consumer identity",
               "required_timelinize_constraints": ["timestamp", "data", "data_source_name", "classification_name", "intermediate_location"] if target == "timelinize" else None,
               "partial": True, "originals_modified": False}
    save(out / "report.json", summary)
    with zipfile.ZipFile(out / "archive.zip", "x", compression=zipfile.ZIP_STORED) as package:
        for f in sorted(archive.rglob("*")):
            if f.is_file():
                entry = zipfile.ZipInfo(f.relative_to(archive).as_posix(), (1980, 1, 1, 0, 0, 0))
                entry.external_attr = 0o100600 << 16
                with f.open("rb") as src, package.open(entry, "w") as dst:
                    shutil.copyfileobj(src, dst)
    (out / "archive.zip").chmod(0o600)
    for bundle in [a, b]:
        bundle.verify()
    save(out / "package-sha256.json", {"archive.zip": file_hash(out / "archive.zip")})
    (out / "INCOMPLETE").unlink()
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--earlier", required=True, type=Path)
    p.add_argument("--later", required=True, type=Path)
    p.add_argument("--earlier-source", required=True, type=Path)
    p.add_argument("--later-source", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--verified-media-cache", type=Path, help="prior complete union; reuse media hashes only, with unchanged source stats and fresh copied-byte verification")
    p.add_argument("--earlier-label", default="earlier")
    p.add_argument("--later-label", default="later")
    p.add_argument("--target", choices=["normalized", "meta-json", "timelinize"], default="normalized")
    a = p.parse_args()
    os.umask(0o077)
    cache = {}
    if a.verified_media_cache:
        previous = a.verified_media_cache
        if (previous / "INCOMPLETE").exists() or file_hash(previous / "archive.zip") != json.loads((previous / "package-sha256.json").read_text())["archive.zip"]:
            raise Refusal("invalid_media_cache")
        for uri, entry in json.loads((previous / "media-manifest.json").read_text()).items():
            if entry["source"] in cache and cache[entry["source"]]["sha256"] != entry["sha256"]:
                raise Refusal("media_cache_conflict")
            cache[entry["source"]] = {"sha256": entry["sha256"], "copy": previous / "archive" / uri}
    earlier = Bundle(a.earlier_label, a.earlier, a.earlier_source, cache)
    later = Bundle(a.later_label, a.later, a.later_source, cache)
    if a.earlier_label == a.later_label or not all(__import__("re").fullmatch(r"[A-Za-z0-9_-]+", label) for label in [a.earlier_label, a.later_label]):
        raise Refusal("invalid_generation_labels")
    print(json.dumps(publish(earlier, later, a.output, a.target), indent=2))


def cli():
    try:
        main()
    except (Refusal, OSError, UnicodeError, ValueError, KeyError) as error:
        print(json.dumps({"error": str(error) if isinstance(error, Refusal) else type(error).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(cli())
