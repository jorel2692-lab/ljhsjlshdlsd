#!/usr/bin/env python3
"""Push F00H files to kioka8877-ux/LACRIMAE via the GitHub REST API.

Méthode (contournement Freebuff décrite par l'utilisateur) :
    blobs -> tree -> commit -> ref, sans passer par git HTTPS.

Usage :
    KIOKA_PAT=<token> python3 tools/push_kioka.py <branch> <file1> [file2 ...]

Le token se lit depuis l'env KIOKA_PAT (Settings -> Environment).
Les fichiers sont poussés à la racine de la branche cible :
    dev10     : F00H/PREVIEW/index.html + modal/workers/f00h_hook_worker.py
    gh-pages  : f00h-preview/index.html (outil servi par GitHub Pages)
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com/repos/kioka8877-ux/LACRIMAE"


def gh(method, path, payload=None):
    req = urllib.request.Request(API + path, method=method)
    req.add_header("Authorization", f"token {os.environ['KIOKA_PAT']}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "f00h-push")
    data = json.dumps(payload).encode() if payload is not None else None
    try:
        with urllib.request.urlopen(req, data=data) as r:
            body = r.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        print("HTTP ERROR", e.code, path, e.read().decode()[:500])
        raise


def get_tree(sha):
    return gh("GET", f"/git/trees/{sha}?recursive=1")["tree"]


def create_blob(content):
    return gh("POST", "/git/blobs", {"content": content, "encoding": "utf-8"})["sha"]


def create_tree(entries):
    return gh("POST", "/git/trees", {"tree": entries})["sha"]


def create_commit(tree_sha, parent_sha, message):
    return gh("POST", "/git/commits", {"message": message, "tree": tree_sha, "parents": [parent_sha]})["sha"]


def update_ref(branch, sha):
    gh("PATCH", f"/git/refs/heads/{branch}", {"sha": sha, "force": False})


def rebuild_tree(recursive_entries, replace_map):
    """Reconstruit l'arbre en remplaçant les blobs de replace_map {path: blob_sha}."""
    from collections import defaultdict
    children = defaultdict(dict)
    root = {}
    for e in recursive_entries:
        p, typ, sha = e["path"], e["type"], e["sha"]
        if "/" not in p:
            root[p] = (typ, sha)
        else:
            parent, name = p.rsplit("/", 1)
            children[parent][name] = (typ, sha)
    new_tree = {}
    for parent, kids in sorted(children.items(), key=lambda kv: -kv[0].count("/")):
        entries = []
        for name, (typ, sha) in kids.items():
            path = parent + "/" + name
            if path in replace_map:
                entries.append({"path": name, "mode": "100644", "type": "blob", "sha": replace_map[path]})
            elif typ == "blob":
                entries.append({"path": name, "mode": "100644", "type": "blob", "sha": sha})
            else:
                entries.append({"path": name, "mode": "040000", "type": "tree", "sha": new_tree[path]})
        nt = create_tree(entries)
        new_tree[parent] = nt
        if "/" in parent:
            gp, gn = parent.rsplit("/", 1)
            children[gp][gn] = ("tree", nt)
        else:
            root[parent] = ("tree", nt)
    entries = []
    for name, (typ, sha) in root.items():
        mode = "040000" if typ == "tree" else "100644"
        entries.append({"path": name, "mode": mode, "type": typ, "sha": sha})
    return create_tree(entries)


def push_branch(branch, replace_map, message):
    ref = gh("GET", f"/git/refs/heads/{branch}")
    parent_sha = ref["object"]["sha"]
    commit = gh("GET", f"/git/commits/{parent_sha}")
    tree = get_tree(commit["tree"]["sha"])
    new_tree_sha = rebuild_tree(tree, replace_map)
    new_commit = create_commit(new_tree_sha, parent_sha, message)
    update_ref(branch, new_commit)
    print(f"OK {branch} -> {new_commit}")


def main():
    branch = sys.argv[1]
    files = sys.argv[2:]
    if not files:
        print("usage: push_kioka.py <branch> <file> [file...]")
        sys.exit(1)
    blobs = {}
    for f in files:
        content = open(f, encoding="utf-8").read()
        blobs[f] = create_blob(content)
        print("blob:", f, len(content), "chars")
    push_branch(branch, blobs, "feat(F00H): mise a jour outil preview / worker format de sortie")


if __name__ == "__main__":
    main()