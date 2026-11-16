"""Versioned Unicode interchange and explicitly selected consumer adapters."""
from copy import deepcopy

FORMAT = "facebook-archive-normalized/1"


def normalized(files, provenance):
    """Own contract, not an official Meta schema. Locations are not platform IDs."""
    profile = files["profile_information/profile_information.json"]["profile_v2"]
    document = {"format": FORMAT, "text_encoding": "Unicode", "profile": {
        "name": profile["name"]["full_name"], "username": profile["username"]},
        "posts": [], "threads": [], "posted_media": [], "provenance": provenance}
    for path, obj in sorted(files.items()):
        if path.startswith("posts/your_posts_"):
            for i, post in enumerate(obj):
                attachments = []
                for group in post["attachments"]:
                    for a in group["data"]:
                        if "media" in a:
                            attachments.append({"kind": "media", **deepcopy(a["media"])})
                        elif "external_context" in a:
                            c = a["external_context"]
                            attachments.append({"kind": "link", "url": c["url"], "label": c["name"]})
                document["posts"].append({"location": f"{path}#/{i}", "timestamp": post["timestamp"],
                    "action_title": post["title"], "text": [d["post"] for d in post["data"] if "post" in d],
                    "attachments": attachments})
        elif path.startswith("messages/"):
            thread = {"location": path, "observed_senders": [p["name"] for p in obj["participants"]], "messages": []}
            for i, msg in enumerate(obj["messages"]):
                attachments = [{"kind": kind, **deepcopy(a)} for field, kind in [("photos", "image"), ("videos", "video")]
                               for a in msg.get(field, [])]
                thread["messages"].append({"location": f"{path}#/messages/{i}", "sender": msg["sender_name"],
                    "timestamp_ms": msg["timestamp_ms"], "text": msg["content"], "attachments": attachments})
            document["threads"].append(thread)
        elif path == "posts/your_uncategorized_photos.json":
            for i, item in enumerate(obj["other_photos_v2"]):
                document["posted_media"].append({"location": f"{path}#/other_photos_v2/{i}", **deepcopy(item)})
    return document


def modern_files(document):
    """Reconstruct the observed compatibility subset without changing Unicode."""
    if document["format"] != FORMAT or document["text_encoding"] != "Unicode":
        raise ValueError("unsupported_normalized_format")
    p = document["profile"]
    files = {"profile_information/profile_information.json": {"profile_v2": {
        "name": {"full_name": p["name"]}, "username": p["username"]}}}
    posts = []
    for post in document["posts"]:
        attachments = []
        for a in post["attachments"]:
            if a["kind"] == "media":
                attachments.append({"media": {k: v for k, v in a.items() if k != "kind"}})
            else:
                attachments.append({"external_context": {"url": a["url"], "name": a["label"]}})
        posts.append({"timestamp": post["timestamp"], "title": post["action_title"],
            "data": [{"post": text} for text in post["text"]],
            "attachments": [{"data": attachments}] if attachments else []})
    files["posts/your_posts_1.json"] = posts
    for thread in document["threads"]:
        messages = []
        for msg in thread["messages"]:
            result = {"sender_name": msg["sender"], "timestamp_ms": msg["timestamp_ms"], "content": msg["text"]}
            for a in msg["attachments"]:
                field = {"image": "photos", "video": "videos"}[a["kind"]]
                result.setdefault(field, []).append({k: v for k, v in a.items() if k != "kind"})
            messages.append(result)
        files[thread["location"]] = {"participants": [{"name": n} for n in thread["observed_senders"]], "messages": messages}
    if document["posted_media"]:
        files["posts/your_uncategorized_photos.json"] = {"other_photos_v2": [
            {k: v for k, v in item.items() if k != "location"} for item in document["posted_media"]]}
    return files


def compatibility(files, target):
    """Timelinize fixes byte-escaped UTF-8 only on specific string fields."""
    result = deepcopy(files)
    if target == "meta-json":
        return result
    if target != "timelinize":
        raise ValueError("unsupported_compatibility_target")
    def escaped(text):
        return text.encode("utf-8").decode("latin1")
    for path, obj in result.items():
        if path.startswith("profile_information/"):
            obj["profile_v2"]["name"]["full_name"] = escaped(obj["profile_v2"]["name"]["full_name"])
        elif path.startswith("messages/"):
            for participant in obj["participants"]:
                participant["name"] = escaped(participant["name"])
            for message in obj["messages"]:
                for field in ["sender_name", "content"]:
                    message[field] = escaped(message[field])
        elif path.startswith("posts/your_posts_"):
            for post in obj:
                for data in post["data"]:
                    if "post" in data:
                        data["post"] = escaped(data["post"])
        elif path == "posts/your_uncategorized_photos.json":
            for media in obj["other_photos_v2"]:
                for field in ["title", "description"]:
                    media[field] = escaped(media[field])
    return result


def media_location(uri, target):
    if target != "normalized" and uri.startswith("media/"):
        return "your_facebook_activity/posts/media/" + uri[len("media/"):]
    return uri


def relocate_media(value, target):
    if isinstance(value, dict):
        return {k: media_location(v, target) if k == "uri" else relocate_media(v, target) for k, v in value.items()}
    if isinstance(value, list):
        return [relocate_media(v, target) for v in value]
    return value
