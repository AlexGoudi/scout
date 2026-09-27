from collections import defaultdict
import json
import pprint

class Subset:
    def __init__(self):
        self.groups_of=defaultdict(set)
        self.members_of=defaultdict(set)

    # read the json file to load all the groups and paths belonging to each group
    def load(self):
        with open('scout_impl/static/datapath.json','r') as json_file:
            data=json.load(json_file)
            for group_name in data.__iter__():
                for path in data[group_name]:
                    self.groups_of[path].add(group_name)
                    self.members_of[group_name].add(path)

    # for a given path or paths, collect all the groups they exist in, if all paths belong to specific groups return only those
    # if they don't belong to the same groups, return all the groups
    def query_groups(self,*strings):
        memberships=[self.groups_of[s] for s in strings]
        # memberships=[m for m in memberships if m]
        if not memberships:
            return set()
        shared=set.intersection(*memberships)
        return shared if shared else set.union(*memberships)

    # for the given paths, after finding the groups, return all the paths included in the groups in json format
    def query_related(self,*strings):
        groups=self.query_groups(*strings)
        related=set()
        for g in groups:
            related|=self.members_of[g]
        return_list= list({i: i for i in related}.values())
        return json.dumps(return_list)


def  main():

    s=Subset()
    s.load()
    print(s.query_related("sonic-mgmt/spytest/templates/show_ip_arp.tmpl"))

if __name__=="__main__":
    main()