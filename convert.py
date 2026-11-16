#!/usr/bin/env python3
"""Offline, fail-closed adapter for observed English Facebook HTML archives.

No network, source writes, platform IDs, timezone defaults, or imports.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import zipfile
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

RECIPE = "facebook-html-4"
FORMAT = "facebook-archive-normalized/1"
VOID = set("area base br col embed hr img input link meta param source track wbr".split())
BLOCK = set("div p li tr h1 h2 h3 section footer".split())
# Only these attachment types have observed supported layout coverage.
MEDIA = {".jpg", ".jpeg", ".png", ".gif", ".mp4", ".mov"}


class Refusal(ValueError):
    pass


class Node:
    def __init__(self, tag="", attrs=()):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def has(self, cls):
        return cls in self.attrs.get("class", "").split()

    def nodes(self):
        return [c for c in self.children if isinstance(c, Node)]

    def text(self):
        if self.tag in {"script", "style"}:
            return ""
        if self.tag == "br":
            return "\n"
        if self.tag == "img":
            return self.attrs.get("alt", "")
        return "".join((c.text() + ("\n" if c.tag in BLOCK else ""))
                       if isinstance(c, Node) else c for c in self.children).strip("\n")


class DOM(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def select(node, cls):
    return [n for n in node.walk() if n.has(cls)]


def single(nodes, reason):
    if len(nodes) != 1:
        raise Refusal(reason)
    return nodes[0]


def declared_timezone(dom):
    boxes = [b for b in select(dom, "_2lej")
             if any(n.text().strip() == "Timezone" for n in select(b, "_2lel"))]
    box = single(boxes, "export_timezone_layout")
    value = single(select(box, "_2let"), "export_timezone_layout").text().strip()
    try:
        return ZoneInfo(value)
    except (ValueError, KeyError):
        raise Refusal("invalid_export_timezone") from None


def legacy(text):
    """Compatibility identity; parser keeps Unicode. See formats.py for adapters."""
    return text


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory(root):
    """No symlink traversal; stat manifest includes unconverted files too."""
    result = {}
    def fail(error):
        raise error
    for base, dirs, files in os.walk(root, followlinks=False, onerror=fail):
        dirs.sort()
        for name in dirs:
            p = Path(base) / name
            if p.is_symlink():
                raise Refusal("source_symlink")
        for name in sorted(files):
            p = Path(base) / name
            st = p.lstat()
            if stat.S_ISLNK(st.st_mode):
                raise Refusal("source_symlink")
            if not stat.S_ISREG(st.st_mode):
                raise Refusal("source_nonregular_file")
            result[p.relative_to(root).as_posix()] = {"bytes": st.st_size, "mtime_ns": st.st_mtime_ns}
    return result


def timestamp(node, zone=None, offset=None):
    """Return explicit epoch or an actionable error, retaining original text."""
    printed = node.text().strip()
    epochs = [n.attrs["data-utime"] for n in node.walk() if "data-utime" in n.attrs]
    if epochs:
        if len(set(epochs)) != 1 or not re.fullmatch(r"\d{9,11}", epochs[0]):
            raise Refusal("invalid_epoch")
        return int(epochs[0]), printed, "embedded_epoch"
    try:
        dt = datetime.fromisoformat(printed.replace("Z", "+00:00"))
    except ValueError:
        match = re.fullmatch(r"([A-Za-z]{3}) (\d{1,2}), (\d{4}),? (\d{1,2}):(\d{2})(?::(\d{2}))?\s*([APap][Mm])", printed)
        if not match:
            raise Refusal("unsupported_date") from None
        mon, day, year, hour, minute, second, ampm = match.groups()
        months = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
        if mon not in months or not 1 <= int(hour) <= 12:
            raise Refusal("unsupported_date")
        try:
            dt = datetime(int(year), months.index(mon)+1, int(day),
                          int(hour) % 12 + (12 if ampm.lower() == "pm" else 0),
                          int(minute), int(second or 0))
        except ValueError:
            raise Refusal("unsupported_date") from None
    if dt.tzinfo is not None:
        return int(dt.timestamp()), printed, "explicit_offset"
    if offset is not None:
        return int(dt.replace(tzinfo=offset).timestamp()), printed, "operator_fixed_offset"
    if zone is None:
        raise Refusal("timezone_required")
    candidates = {int(dt.replace(tzinfo=zone, fold=fold).timestamp()) for fold in (0, 1)
                  if dt.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == dt}
    if not candidates:
        raise Refusal("nonexistent_local_time")
    if len(candidates) != 1:
        raise Refusal("ambiguous_local_time")
    return candidates.pop(), printed, "operator_timezone"


def update_date_label(text, zone=None, offset=None):
    if not text.startswith("Updated "):
        return False
    node = Node()
    node.children = [text[len("Updated "):]]
    try:
        timestamp(node, zone, offset)
    except Refusal as e:
        return str(e) in {"timezone_required", "ambiguous_local_time", "nonexistent_local_time"}
    return True


class Conversion:
    def __init__(self, source, zone=None, offset=None):
        self.source = Path(source).resolve(strict=True)
        if not self.source.is_dir() or not (self.source / "index.html").is_file():
            raise Refusal("expected_export_root_with_index")
        self.before = inventory(self.source)
        if any(p.lower().endswith(".json") for p in self.before):
            raise Refusal("existing_json_requires_review")
        self.zone, self.offset = zone, offset
        self.files, self.reads, self.media, self.provenance, self.issues = {}, {}, {}, [], []
        self.counts = Counter()
        self.export_zone = None
        if "location/timezone.html" in self.before:
            self.export_zone = declared_timezone(self.read("location/timezone.html"))
            if offset is not None or (zone is not None and str(zone) != str(self.export_zone)):
                raise Refusal("requested_timezone_conflicts_with_export_metadata")
            self.zone = self.export_zone

    def read(self, rel):
        raw = (self.source / rel).read_bytes()
        self.reads[rel] = digest(raw)
        return DOM(raw.decode("utf-8-sig")).root

    def issue(self, rel, index, reason):
        self.issues.append({"path": rel, "index": index, "reason": reason})
        if self.provenance and self.provenance[-1].get("path") == rel and self.provenance[-1].get("index") == index:
            self.provenance[-1]["status"] = "refused"

    def media_ref(self, ref):
        try:
            parts = urlsplit(ref)
        except ValueError:
            raise Refusal("invalid_reference") from None
        if parts.scheme or parts.netloc:
            return None
        decoded = unquote(parts.path)
        if not decoded or decoded.startswith("/") or "\\" in decoded or "\x00" in decoded or ".." in Path(decoded).parts:
            raise Refusal("unsafe_media_path")
        # These exports declare base href="../.." etc; references are root-relative.
        p = self.source / decoded
        if decoded not in self.before or not p.is_file():
            raise Refusal("missing_media")
        if p.resolve() != p or not p.is_relative_to(self.source):
            raise Refusal("unsafe_media_path")
        if p.suffix.lower() not in MEDIA:
            raise Refusal("unsupported_attachment_type")
        # Derived location only; the consumer adapter may relocate this prefix.
        target = "media/" + decoded
        self.media[target] = p
        return target

    def attachments(self, node, epoch):
        result, seen = [], set()
        for n in node.walk():
            if n.tag not in {"a", "img", "video", "audio", "source"}:
                continue
            ref = n.attrs.get("href" if n.tag == "a" else "src", "")
            if not ref or ref.startswith("#"):
                continue
            try:
                parts = urlsplit(ref)
            except ValueError:
                raise Refusal("invalid_reference") from None
            if parts.scheme or parts.netloc:
                # External image/emoticon is not fetched; preserve its alt text.
                if n.tag == "a" and parts.scheme in {"http", "https"} and ref not in seen:
                    result.append({"external_context": {"url": ref, "name": n.text()}})
                    seen.add(ref)
                elif n.tag != "a":
                    self.counts["remote_media_unfetched"] += 1
                continue
            uri = self.media_ref(ref)
            if uri not in seen:
                result.append({"media": {"uri": uri, "creation_timestamp": epoch}})
                seen.add(uri)
        return result

    def record(self, rel, index, footer, output, fields):
        try:
            epoch, printed, basis = timestamp(footer, self.zone, self.offset)
        except Refusal as e:
            self.issue(rel, index, str(e))
            epoch, printed, basis = None, footer.text(), "unresolved"
        self.provenance.append({"source_sha256": self.reads[rel], "path": rel,
                                "selector": "._2lej", "index": index, "recipe": RECIPE,
                                "original_timestamp": printed, "timestamp_basis": basis,
                                "normalized_epoch": epoch,
                                "timestamp_attributes": [{"data-utime": n.attrs["data-utime"]} for n in footer.walk() if "data-utime" in n.attrs],
                                "timestamp_precision": "seconds" if re.search(r":\d\d:\d\d", printed) else "minutes",
                                "output": output, "normalized_fields": fields})
        self.provenance[-1]["status"] = "refused" if epoch is None else "parsed"
        # None remains explicit in memory; write() refuses all unresolved records.
        return epoch

    def profile(self):
        rel = "profile_information/profile_information.html"
        if rel not in self.before:
            raise Refusal("profile_missing")
        dom = self.read(rel)
        fields = {}
        for row in dom.walk():
            if row.tag != "tr":
                continue
            labels = [n for n in row.nodes() if n.tag == "th"]
            values = [n for n in row.nodes() if n.tag == "td"]
            if len(labels) == len(values) == 1 and labels[0].text().strip() in {"Name", "Username"}:
                label = labels[0].text().strip()
                if label in fields:
                    raise Refusal("ambiguous_profile")
                fields[label] = values[0].text().strip()
        if not fields.get("Name") or not fields.get("Username"):
            raise Refusal("profile_identity_missing")
        self.owner = fields["Name"]
        self.files["profile_information/profile_information.json"] = {"profile_v2": {
            "name": {"full_name": legacy(self.owner)}, "username": fields["Username"]}}
        self.provenance.append({"source_sha256": self.reads[rel], "path": rel,
                                "selector": "tr/th[Name|Username]", "recipe": RECIPE,
                                "normalized_fields": ["profile_v2.name.full_name", "profile_v2.username"]})

    def posts(self):
        paths = sorted(p for p in self.before if re.fullmatch(r"posts/your_posts(?:_\d+)?\.html", p))
        if "posts/your_posts.html" in paths and len(paths) > 1:
            raise Refusal("ambiguous_posts_generations")
        posts = []
        for rel in paths:
            dom = self.read(rel)
            boxes = select(dom, "_2lej")
            self.counts["post_records"] += len(boxes)
            if not boxes:
                self.issue(rel, None, "unknown_post_layout")
            for i, box in enumerate(boxes):
                try:
                    footer = single(select(box, "_2lem"), "post_footer_layout")
                    epoch = self.record(rel, i, footer, f"posts/your_posts_1.json#/{len(posts)}",
                                        ["timestamp", "title", "data.post", "attachments"])
                    if epoch is None:
                        continue
                    title = select(box, "_2lel")
                    bodies = select(box, "_2let")
                    if len(bodies) > 1 or len(title) > 1:
                        raise Refusal("post_body_layout")
                    words, attachments = [], []
                    if bodies:
                        cells = select(bodies[0], "_2pin")
                        if not cells and bodies[0].text().strip():
                            raise Refusal("unknown_post_body")
                        for cell in cells:
                            for child in cell.nodes():
                                # Own text has only inline markup; nested divs are shared content.
                                if any(n.tag in {"div", "table", "video", "audio"} for n in list(child.walk())[1:]):
                                    attachments += self.attachments(child, epoch)
                                    self.counts["post_structured_cells"] += 1
                                else:
                                    text = child.text().strip()
                                    if update_date_label(text, self.zone, self.offset):
                                        self.counts["post_update_labels"] += 1
                                    elif text:
                                        words.append(text)
                                    attachments += self.attachments(child, epoch)
                    text = "\n\n".join(words)
                    posts.append({"timestamp": epoch, "title": title[0].text() if title else "",
                                  "data": [{"post": legacy(text)}] if text else [],
                                  "attachments": [{"data": attachments}] if attachments else []})
                    self.counts["posts_converted"] += 1
                    if not text and not attachments:
                        self.counts["posts_without_content"] += 1
                except Refusal as e:
                    self.issue(rel, i, str(e))
        self.files["posts/your_posts_1.json"] = posts

    def messages(self):
        paths = sorted(p for p in self.before if re.fullmatch(
            r"messages/(?:(?:inbox|archived_threads|filtered_threads|message_requests)/)?[^/]+/message(?:_\d+)?\.html", p))
        threads = {}
        for rel in paths:
            thread = threads.setdefault(str(Path(rel).parent), {"messages": [], "senders": set(), "paths": []})
            thread["paths"].append(rel)
            dom = self.read(rel)
            boxes = select(dom, "_2lej")
            self.counts["message_records"] += len(boxes)
            if not boxes:
                self.issue(rel, None, "unknown_message_layout")
            for i, box in enumerate(boxes):
                try:
                    headers = select(box, "_2lel")
                    # Some old exports put thread metadata in the same box class.
                    if not headers:
                        self.counts["message_metadata_boxes_unconverted"] += 1
                        continue
                    sender = single(headers, "message_sender_layout").text().strip()
                    if not sender:
                        raise Refusal("empty_sender")
                    footer = single(select(box, "_2lem"), "message_footer_layout")
                    token = digest(str(Path(rel).parent).encode())[:24]
                    epoch = self.record(rel, i, footer, f"messages/inbox/{token}/message_1.json#/messages/{len(thread['messages'])}",
                                        ["sender_name", "timestamp_ms", "content", "attachments"])
                    if epoch is None:
                        continue
                    body = single(select(box, "_2let"), "message_body_layout")
                    wrappers = body.nodes()
                    if len(wrappers) != 1 or len(wrappers[0].nodes()) < 4:
                        raise Refusal("unknown_message_body")
                    attachment_node, text_node, share_node, reaction_node = wrappers[0].nodes()[:4]
                    if reaction_node.text().strip():
                        # Layout alone cannot prove reaction actor/value delimiters.
                        self.counts["message_reaction_blocks_unconverted"] += 1
                    msg = {"sender_name": legacy(sender), "timestamp_ms": epoch * 1000 if epoch is not None else None,
                           "content": legacy(text_node.text().strip())}
                    attachments = self.attachments(attachment_node, epoch)
                    for extra in wrappers[0].nodes()[4:]:
                        if select(extra, "_tqp"):
                            # Observed extra reaction list; do not guess actor delimiters.
                            self.counts["message_reaction_blocks_unconverted"] += 1
                            continue
                        ip_label = re.fullmatch(r"IP Address: (\S+)", extra.text().strip(), re.I)
                        if ip_label:
                            try:
                                ipaddress.ip_address(ip_label[1])
                            except ValueError:
                                raise Refusal("invalid_message_ip_annotation") from None
                            self.counts["message_ip_annotations_provenance_only"] += 1
                            continue
                        # Additional exported attachment slots are supported only
                        # when every nonblank text leaf belongs to an attachment.
                        # Plain annotations/events require a separate recipe.
                        def attachment_only(n):
                            if n.tag in {"a", "img", "video", "audio", "source"}:
                                return True
                            return all(attachment_only(c) if isinstance(c, Node)
                                       else not c.strip() for c in n.children)
                        if not attachment_only(extra):
                            raise Refusal("unmapped_message_annotation")
                        attachments += self.attachments(extra, epoch)
                    attachments += self.attachments(text_node, epoch)
                    shares = self.attachments(share_node, epoch)
                    attachments += shares
                    links = []
                    for attachment in attachments:
                        if "external_context" in attachment:
                            link = attachment["external_context"]["url"]
                            if link not in links and link not in text_node.text():
                                links.append(link)
                            continue
                        media = attachment["media"]
                        ext = Path(media["uri"]).suffix.lower()
                        field = "videos" if ext in {".mp4", ".mov"} else "photos"
                        if media not in msg.setdefault(field, []):
                            msg[field].append(media)
                    if links:
                        msg["content"] += legacy("\n\n" + "\n".join(links))
                    if not msg["content"] and not any(msg.get(k) for k in ["photos", "videos", "audio_files"]):
                        self.counts["empty_messages_unconverted"] += 1
                        self.provenance[-1]["status"] = "empty"
                        continue
                    thread["messages"].append(msg)
                    thread["senders"].add(sender)
                    self.counts["messages_converted"] += 1
                except Refusal as e:
                    self.issue(rel, i, str(e))
        for key, thread in sorted(threads.items()):
            if not thread["messages"]:
                continue
            # Preserve thread separation without claiming this hash is a Meta ID.
            token = digest(key.encode())[:24]
            self.files[f"messages/inbox/{token}/message_1.json"] = {
                "participants": [{"name": legacy(n)} for n in sorted(thread["senders"])],
                "messages": thread["messages"]}
            self.counts["threads_converted"] += 1
        self.counts["thread_participants_from_observed_senders_only"] = self.counts["threads_converted"]

    def run(self):
        self.profile()
        self.posts()
        self.messages()
        self.standalone_media()
        def uris(obj):
            if isinstance(obj, dict):
                for k, v in obj.items():
                    if k == "uri" and isinstance(v, str):
                        yield v
                    else:
                        yield from uris(v)
            elif isinstance(obj, list):
                for value in obj:
                    yield from uris(value)
        referenced = set(uris(self.files))
        self.media = {k: v for k, v in self.media.items() if k in referenced}
        return self

    def standalone_media(self):
        """Posted media, including album members; membership stays in provenance.

        Do not interpret upload metadata tables as captions or EXIF. The footer
        is the exported posting date, not a claimed camera capture timestamp.
        """
        entries = []
        paths = sorted(p for p in self.before if re.fullmatch(
            r"photos_and_videos/(?:your_photos|your_videos|album/\d+)\.html", p))
        for rel in paths:
            boxes = select(self.read(rel), "_2lej")
            for i, box in enumerate(boxes):
                self.counts["standalone_media_records"] += 1
                try:
                    footer = single(select(box, "_2lem"), "media_footer_layout")
                    epoch = self.record(rel, i, footer,
                                        f"posts/your_uncategorized_photos.json#/other_photos_v2/{len(entries)}",
                                        ["uri", "creation_timestamp", "title", "description"])
                    if epoch is None:
                        continue
                    body = single(select(box, "_2let"), "media_body_layout")
                    primary = [n for n in body.nodes() if n.tag in {"a", "video", "img"}]
                    node = single(primary, "media_primary_layout")
                    ref = node.attrs.get("href" if node.tag == "a" else "src", "")
                    uri = self.media_ref(ref)
                    if not uri:
                        raise Refusal("remote_standalone_media")
                    title = select(box, "_2lel")
                    if len(title) > 1:
                        raise Refusal("media_title_layout")
                    captions = [n.text() for n in body.nodes() if n.tag == "div" and n.has("_3-95")]
                    entries.append({"uri": uri, "creation_timestamp": epoch,
                                    "title": legacy(title[0].text()) if title else "",
                                    "description": legacy("\n\n".join(captions))})
                    self.provenance[-1]["album_source"] = rel if "/album/" in rel else None
                    self.provenance[-1]["media_date_meaning"] = "exported posting date; not camera capture time"
                    self.counts["standalone_media_converted"] += 1
                    if "/album/" in rel:
                        self.counts["album_membership_provenance_only"] += 1
                except Refusal as e:
                    self.issue(rel, i, str(e))
        if paths:
            self.files["posts/your_uncategorized_photos.json"] = {"other_photos_v2": entries}

    def report(self):
        handled = set(self.reads)
        omitted = Counter(p.split("/")[0] for p in self.before if p.endswith(".html") and p not in handled)
        return {"recipe": RECIPE, "format": FORMAT, "source_files": len(self.before),
                "source_stat_manifest_sha256": digest(json.dumps(self.before, sort_keys=True).encode()),
                "counts": dict(self.counts), "issues_by_reason": dict(Counter(x["reason"] for x in self.issues)),
                "unconverted_html_by_category": dict(sorted(omitted.items())),
                "referenced_media_files": len(self.media), "publication_blocked": bool(self.issues),
                "timezone": str(self.zone) if self.zone else None,
                "export_timezone": str(self.export_zone) if self.export_zone else None,
                "utc_offset": str(self.offset) if self.offset else None,
                "timezone_basis": "explicit export timezone metadata" if self.export_zone else "explicit operator input; not independently established by HTML",
                "coverage": "own posts, message text and supported image/video references, posted media including album members, profile identity; participants from observed senders only; album membership/upload metadata/IP annotations in source provenance only; reactions, audio, documents and remaining categories unconverted"}

    def evidence(self, output):
        output = Path(output).absolute()
        parent = output.parent.resolve(strict=True)
        output = parent / output.name
        if output == self.source or output.is_relative_to(self.source) or self.source.is_relative_to(output):
            raise Refusal("source_output_overlap")
        output.mkdir(mode=0o700, exist_ok=False)
        for name, obj in [("report.json", self.report()), ("issues.json", self.issues),
                          ("provenance.json", self.provenance),
                          ("source-manifest.json", {"files": self.before, "parsed_html_sha256": self.reads})]:
            p = output / name
            with p.open("x", encoding="utf-8") as f:
                os.chmod(p, 0o600)
                json.dump(obj, f, indent=2, ensure_ascii=True)

    def unchanged(self):
        if inventory(self.source) != self.before:
            raise Refusal("source_stat_changed")
        for rel, expected in self.reads.items():
            if digest((self.source / rel).read_bytes()) != expected:
                raise Refusal("source_html_changed")

    def write(self, output, media_mode="reference", allow_partial=False, target="normalized"):
        from formats import normalized, compatibility, relocate_media, media_location
        if target not in {"normalized", "meta-json", "timelinize"} or media_mode not in {"reference", "copy"}:
            raise Refusal("unknown_output_profile")
        output = Path(output).absolute()
        parent = output.parent.resolve(strict=True)
        output = parent / output.name
        if output == self.source or output.is_relative_to(self.source) or self.source.is_relative_to(output):
            raise Refusal("source_output_overlap")
        if output.exists() or output.is_symlink():
            raise Refusal("output_exists")
        if self.issues and not allow_partial:
            raise Refusal("unresolved_records_no_output")
        self.unchanged()
        output.mkdir(mode=0o700)
        # A failure leaves a private, clearly incomplete directory, never an import target.
        (output / "INCOMPLETE").touch(mode=0o600)
        archive = output / "archive"
        archive.mkdir(mode=0o700)
        def save(path, obj):
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as f:
                os.chmod(path, 0o600)
                json.dump(obj, f, ensure_ascii=True, indent=2)
                f.write("\n")
        files = relocate_media(self.files, target)
        save(output / "normalized.json", normalized(files, self.provenance))
        if target == "normalized":
            save(archive / "normalized.json", normalized(files, self.provenance))
        if target != "normalized":
            for rel, obj in compatibility(files, target).items():
                save(archive / rel, obj)
        media_hashes = {}
        for rel, source in sorted(self.media.items()):
            destination = archive / media_location(rel, target)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if media_mode == "copy":
                shutil.copyfile(source, destination)
                destination.chmod(0o600)
                expected = file_hash(source)
                if file_hash(destination) != expected:
                    raise Refusal("media_copy_mismatch")
                media_hashes[media_location(rel, target)] = expected
            else:
                destination.symlink_to(source)
        save(output / "provenance.json", self.provenance)
        save(output / "refused-records.json", self.issues)
        save(output / "source-manifest.json", {"files": self.before, "parsed_html_sha256": self.reads,
                                               "copied_media_sha256": media_hashes,
                                               "media_sources": {media_location(k, target): p.relative_to(self.source).as_posix() for k, p in self.media.items()}})
        save(output / "report.json", {**self.report(), "target": target, "media_mode": media_mode,
                                       "partial_output_explicitly_allowed": allow_partial})
        if media_mode == "copy":
            # Root-level JSON paths, real converted records, only referenced media.
            # Reference-mode symlinks deliberately remain local and unpackaged.
            with zipfile.ZipFile(output / "archive.zip", "x", compression=zipfile.ZIP_STORED) as package:
                for path in sorted(archive.rglob("*")):
                    if path.is_file():
                        entry = zipfile.ZipInfo(path.relative_to(archive).as_posix(), (1980, 1, 1, 0, 0, 0))
                        entry.external_attr = 0o100600 << 16
                        with path.open("rb") as src, package.open(entry, "w") as dst:
                            shutil.copyfileobj(src, dst)
            (output / "archive.zip").chmod(0o600)
        self.unchanged()
        (output / "INCOMPLETE").unlink()


def parse_offset(value):
    if not re.fullmatch(r"[+-]\d{2}:\d{2}", value):
        raise argparse.ArgumentTypeError("use signed HH:MM")
    hours, minutes = map(int, value[1:].split(":"))
    if hours > 23 or minutes > 59:
        raise argparse.ArgumentTypeError("invalid UTC offset")
    return timezone((1 if value[0] == "+" else -1) * timedelta(hours=hours, minutes=minutes))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, help="new private directory; omit for read-only plan")
    parser.add_argument("--evidence", type=Path, help="new private report/provenance directory, even when blocked")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--source-timezone", type=ZoneInfo)
    group.add_argument("--source-utc-offset", type=parse_offset)
    parser.add_argument("--target", choices=["normalized", "meta-json", "timelinize"], default="normalized", help="normalized Unicode by default; compatibility outputs also retain normalized.json")
    parser.add_argument("--media", choices=["reference", "copy"], default="reference")
    parser.add_argument("--allow-partial", action="store_true", help="write reviewable supported subset; quarantine refused records explicitly")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        run = Conversion(args.source, args.source_timezone, args.source_utc_offset).run()
        if args.evidence:
            run.evidence(args.evidence)
        if args.output:
            run.write(args.output, args.media, args.allow_partial, args.target)
        else:
            run.unchanged()
        print(json.dumps(run.report(), indent=2))
        return 2 if run.issues else 0
    except (Refusal, OSError, UnicodeError) as e:
        # Never print exception messages containing private source text or names.
        print(json.dumps({"error": str(e) if isinstance(e, Refusal) else type(e).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
