#!/usr/bin/env python3
"""
Build the iOS Shortcut clients for pixel-1-sync as unsigned .shortcut files.

    python3 tools/build_shortcuts.py [OUT_DIR]

iOS only imports signed shortcuts, so sign each file on a Mac signed into your Apple ID:

    shortcuts sign --mode anyone --input "Send to Pixel.shortcut" --output "Send to Pixel (signed).shortcut"

then AirDrop the signed file to the iPhone. On import, Shortcuts asks for the server
address and token; neither is stored in these files.

Both shortcuts name each photo `[Name]_[yyyyMMdd_HHmmss creation date]`, exactly as docs/iphone-shortcut.md
describes, so they agree with a hand-built client and with each other.
"""

import os
import plistlib
import sys
import uuid

OBJ = "￼"  # placeholder Shortcuts uses for an inline variable


# --- parameter encodings --------------------------------------------------

def var(name):
    return {"Type": "Variable", "VariableName": name}


def out(action, name):
    return {"Type": "ActionOutput", "OutputUUID": action["UUID"], "OutputName": name}


def as_text(ref):
    """The same reference, coerced to Text (needed before text conditions such as contains)."""
    return dict(ref, Aggrandizements=[{"Type": "WFCoercionVariableAggrandizement",
                                       "CoercionItemClass": "WFStringContentItem"}])


def attach(ref):
    return {"Value": ref, "WFSerializationType": "WFTextTokenAttachment"}


def text(*parts):
    """A text field mixing literal strings and variable references."""
    s, ranges = "", {}
    for p in parts:
        if isinstance(p, str):
            s += p
        else:
            ranges["{%d, 1}" % len(s)] = p
            s += OBJ
    return {"Value": {"string": s, "attachmentsByRange": ranges},
            "WFSerializationType": "WFTextTokenString"}


# --- actions --------------------------------------------------------------

class Builder:
    def __init__(self):
        self.actions = []

    def add(self, ident, params=None, output=True):
        params = dict(params or {})
        if output:
            params["UUID"] = str(uuid.uuid4()).upper()
        action = {"WFWorkflowActionIdentifier": "is.workflow.actions." + ident,
                  "WFWorkflowActionParameters": params}
        self.actions.append(action)
        return params

    def text(self, *parts):
        return self.add("gettext", {"WFTextActionText": text(*parts)})

    def set_var(self, name, ref):
        self.add("setvariable", {"WFVariableName": name, "WFInput": attach(ref)}, output=False)

    def append_var(self, name, ref):
        self.add("appendvariable", {"WFVariableName": name, "WFInput": attach(ref)}, output=False)

    def find_photos(self, limit=None):
        """Newest photos first, optionally only the `limit` most recent. No date filter:
        Shortcuts' "is in the last" filter is unreliable and its encoding undocumented."""
        params = {"WFContentItemSortProperty": "Creation Date",
                  "WFContentItemSortOrder": "Latest First",
                  "WFContentItemLimitEnabled": limit is not None}
        if limit is not None:
            params["WFContentItemLimitNumber"] = limit
        return self.add("filter.photos", params)

    def detail(self, prop, ref):
        return self.add("properties.images", {"WFContentItemPropertyName": prop,
                                               "WFInput": attach(ref)})

    def photo_key(self, *wrap):
        """Text action holding `[Name]_[yyyyMMdd_HHmmss]` for the current Repeat Item."""
        name = self.detail("Name", var("Repeat Item"))
        created = self.detail("Creation Date", var("Repeat Item"))
        stamp = self.add("format.date", {"WFDateFormatStyle": "Custom",
                                         "WFDateFormat": "yyyyMMdd_HHmmss",
                                         "WFDate": text(out(created, "Creation Date"))})
        prefix, suffix = (wrap or ("", ""))
        return self.text(prefix, out(name, "Name"), "_", out(stamp, "Formatted Date"), suffix)

    def block(self, ident, start_params):
        """Start a control-flow block; returns a function that closes it."""
        group = str(uuid.uuid4()).upper()
        self.add(ident, dict(start_params, WFControlFlowMode=0, GroupingIdentifier=group),
                 output=False)
        return lambda: self.add(ident, {"WFControlFlowMode": 2, "GroupingIdentifier": group})

    def repeat_each(self, ref):
        return self.block("repeat.each", {"WFInput": attach(ref)})

    def _if(self, ref, condition, **extra):
        """If block. The condition is written in both the legacy single-condition keys and the
        newer WFConditions table (iOS 16+); each iOS version reads the form it knows."""
        cond = dict({"WFCondition": condition,
                     "WFInput": {"Type": "Variable", "Variable": attach(ref)}}, **extra)
        return self.block("conditional", dict(cond, WFConditions={
            "Value": {"WFActionParameterFilterPrefix": 1,
                      "WFActionParameterFilterTemplates": [cond]},
            "WFSerializationType": "WFContentPredicateTableTemplate",
        }))

    def if_contains(self, haystack_ref, needle_ref):
        # a server response is a file to Shortcuts, which offers only "has any value" for it
        return self._if(as_text(haystack_ref), 99,  # contains
                        WFConditionalActionString=text(needle_ref))

    def if_has_value(self, ref):
        return self._if(ref, 100)  # has any value

    def notify_status(self, title):
        """Notification showing the server's /ping summary (done count, buffer size)."""
        ping = self.add("downloadurl", {"WFURL": text(var("Base"), "/ping"), "WFHTTPMethod": "GET"})
        self.add("notification", {"WFNotificationActionTitle": text(title),
                                  "WFNotificationActionBody": text(out(ping, "Contents of URL")),
                                  "WFNotificationActionSound": True}, output=False)

    def put(self, url_parts, body_ref):
        return self.add("downloadurl", {
            "WFURL": text(*url_parts),
            "WFHTTPMethod": "PUT",
            "ShowHeaders": True,
            "WFHTTPHeaders": {
                "Value": {"WFDictionaryFieldValueItems": [{
                    "WFItemType": 0,
                    "WFKey": text("X-Token"),
                    "WFValue": text(var("Token")),
                }]},
                "WFSerializationType": "WFDictionaryFieldValue",
            },
            "WFHTTPBodyType": "File",
            "WFRequestVariable": attach(body_ref),
        })

    def config(self):
        """Base and Token variables, filled in by import questions (actions 0 and 2)."""
        self.set_var("Base", out(self.text("http://PIXEL_ADDRESS:8765"), "Text"))
        self.set_var("Token", out(self.text("TOKEN"), "Text"))

    def post_names(self, photos, endpoint, list_var):
        """Pass 1: send every photo's key to `endpoint`; returns the response action."""
        end = self.repeat_each(out(photos, "Photos"))
        self.append_var("NameList", out(self.photo_key(), "Text"))
        end()
        combined = self.add("text.combine", {"text": attach(var("NameList")),
                                             "WFTextSeparator": "New Lines"})
        response = self.put([var("Base"), endpoint], out(combined, "Combined Text"))
        self.set_var(list_var, out(response, "Contents of URL"))
        return response


IMPORT_QUESTIONS = [
    {"ActionIndex": 0, "Category": "Parameter", "ParameterKey": "WFTextActionText",
     "DefaultValue": "http://PIXEL_ADDRESS:8765",
     "Text": "Server address: http://<Pixel's Tailscale address>:8765"},
    {"ActionIndex": 2, "Category": "Parameter", "ParameterKey": "WFTextActionText",
     "DefaultValue": "",
     "Text": "PHOTO_TOKEN from the Pixel's start-server.sh"},
]


def workflow(actions, glyph, color):
    return {
        "WFWorkflowClientVersion": "2302.0.4",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color,
                           "WFWorkflowIconGlyphNumber": glyph},
        "WFWorkflowImportQuestions": IMPORT_QUESTIONS,
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": [],
        "WFWorkflowActions": actions,
    }


# --- the two shortcuts ----------------------------------------------------

def send_to_pixel(limit):
    b = Builder()
    b.config()
    photos = b.find_photos(limit)
    b.post_names(photos, "/missing", "Missing")
    # "contains" on the response text can over-match (a short name inside a longer one);
    # that only causes an extra upload, which the server answers with "skipped".
    end_loop = b.repeat_each(out(photos, "Photos"))
    key = b.photo_key()
    end_if = b.if_contains(var("Missing"), out(key, "Text"))
    encoded = b.add("urlencode", {"WFEncodeMode": "Encode",  # a text field, not a content input
                                  "WFInput": text(out(key, "Text"))})
    b.put([var("Base"), "/upload?name=", out(encoded, "URL Encoded Text")], var("Repeat Item"))
    end_if()
    end_loop()
    b.notify_status("Send to Pixel finished")
    return workflow(b.actions, glyph=59446, color=4282601983)


def delete_backed_up(limit):
    b = Builder()
    b.config()
    photos = b.find_photos(limit)
    b.post_names(photos, "/safe_to_delete", "Safe")
    end_loop = b.repeat_each(out(photos, "Photos"))
    key = b.photo_key("|", "|")  # the server wraps names as |name| so matches are exact
    end_if = b.if_contains(var("Safe"), out(key, "Text"))
    b.append_var("ToDelete", var("Repeat Item"))
    end_if()
    end_loop()
    end_any = b.if_has_value(var("ToDelete"))
    b.add("deletephotos", {"photos": attach(var("ToDelete"))}, output=False)  # iOS asks to confirm
    end_any()
    return workflow(b.actions, glyph=59511, color=4251333119)


def check(wf):
    """Every referenced output exists earlier, and every block is closed."""
    seen, open_groups = set(), []
    for a in wf["WFWorkflowActions"]:
        p = a["WFWorkflowActionParameters"]

        def walk(x):
            if isinstance(x, dict):
                if x.get("Type") == "ActionOutput":
                    assert x["OutputUUID"] in seen, "forward reference"
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(p)
        if "GroupingIdentifier" in p:
            if p["WFControlFlowMode"] == 0:
                open_groups.append(p["GroupingIdentifier"])
            elif p["WFControlFlowMode"] == 2:
                assert open_groups.pop() == p["GroupingIdentifier"], "unbalanced block"
        if "UUID" in p:
            seen.add(p["UUID"])
    assert not open_groups, "unclosed block"


RECENT = 500  # photos the automatic run checks each time


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    os.makedirs(out_dir, exist_ok=True)
    # The automatic run looks only at recent photos so it finishes within iOS's time limit;
    # the other two are meant to be run by hand, in the foreground, over the whole library.
    for title, wf in (("Send to Pixel", send_to_pixel(RECENT)),
                      ("Send All to Pixel", send_to_pixel(None)),
                      ("Delete Backed-Up Photos", delete_backed_up(None))):
        check(wf)
        path = os.path.join(out_dir, title + ".shortcut")
        with open(path, "wb") as f:
            plistlib.dump(wf, f, fmt=plistlib.FMT_BINARY)
        print("wrote %s (%d actions)" % (path, len(wf["WFWorkflowActions"])))


if __name__ == "__main__":
    main()
