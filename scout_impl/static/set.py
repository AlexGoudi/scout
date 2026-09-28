"""Group membership over `datapath.json`: which named groups a set of paths shares."""

import json
from collections import defaultdict
from pathlib import Path

DATAPATH = Path(__file__).with_name("datapath.json")


class Subset:
    def __init__(self):
        self.groups_of = defaultdict(set)
        self.members_of = defaultdict(set)

    def load(self):
        """Read every group and the paths belonging to it."""
        with open(DATAPATH, encoding="utf-8") as json_file:
            data = json.load(json_file)
        for group_name, paths in data.items():
            for path in paths:
                self.groups_of[path].add(group_name)
                self.members_of[group_name].add(path)

    def query_groups(self, *strings):
        """The groups every given path belongs to; if they share none, every group any of them is in.

        A path in no group contributes an empty set, so it empties the intersection and the
        answer widens to the union. Ignoring such paths instead is the open alternative.
        """
        memberships = [self.groups_of.get(s, set()) for s in strings]
        if not memberships:
            return set()
        shared = set.intersection(*memberships)
        return shared if shared else set.union(*memberships)

    def query_related(self, *strings):
        """Every path in the groups `query_groups` finds, as a sorted JSON list."""
        related = set()
        for group in self.query_groups(*strings):
            related |= self.members_of[group]
        return json.dumps(sorted(related))


def main():
    s = Subset()
    s.load()
    print(s.query_related("sonic-mgmt/spytest/templates/show_ip_arp.tmpl"))


if __name__ == "__main__":
    main()
