#! /usr/bin/env python


"""

Gets or edits conf params given symbol and param name.

"""

import argparse
import commentjson
import shutil

def get_param(confs, symbol, param):
    param_path = param.split(':')
    vals = []
    for conf in confs:
        with open(conf) as f:
            conf_dict = commentjson.load(f)
        pktraders = conf_dict["pktraders"]
        for pktrader in pktraders:
            sec_sym = pktrader["traded_symbol"]
            if symbol is None or symbol == sec_sym:
                param_ix = 0
                section = pktrader
                while param_ix < len(param_path) - 1:
                    section = section[param_path[param_ix]]
                    param_ix += 1
                key = param_path[-1]
                if key in section:
                    val = section[key]
                else:
                    val = "NOTFOUND"
                vals.append((conf, sec_sym, val))
    return vals

def set_param(confs, symbol, param, new_val):
    param_path = param.split(':')
    all_changes = []
    for conf in confs:
        with open(conf) as f:
            conf_dict = commentjson.load(f)
        pktraders = conf_dict["pktraders"]
        changes = []
        for pktrader in pktraders:
            sec_sym = pktrader["traded_symbol"]
            if symbol is None or symbol == sec_sym:
                param_ix = 0
                section = pktrader
                while param_ix < len(param_path) - 1:
                    section = section[param_path[param_ix]]
                    param_ix += 1
                key = param_path[-1]
                if key in section:
                    old_val = section[key]
                else:
                    old_val = "NOTFOUND"
                if isinstance(old_val, bool):
                    if isinstance(new_val, bool):
                        pass
                    elif isinstance(new_val, str) and new_val.lower() == "true":
                        new_val = True
                    elif isinstance(new_val, str) and new_val.lower() == "false":
                        new_val = False
                    else:
                        raise ValueError(f"Invalid boolean value {new_val}")
                elif isinstance(old_val, int):
                    new_val = int(new_val)
                elif isinstance(old_val, float):
                    new_val = float(new_val)
                if old_val != new_val:
                    changes.append(f"{conf} {sec_sym} {param} {old_val} to {new_val}")
                    section[param_path[-1]] = new_val
        if changes:
            ans = input(f"About to backup and edit {conf}. Continue? (y/n) ")
            if ans.lower() in ["y", "yes"]:
                shutil.copy2(conf, conf + ".bak")
                with open(conf, 'w') as f:
                    commentjson.dump(conf_dict, f, indent=2)
                    f.write('\n')
                all_changes += changes
    return all_changes

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--get", action="store_true")
    parser.add_argument("--set", action="store_true")
    parser.add_argument("--symbol")
    parser.add_argument("--param")
    parser.add_argument("--value")
    parser.add_argument("confs", nargs='+')
    args = parser.parse_args()

    if args.get and args.set:
        sys.exit("Use either --get or --set, not both.")
    if not args.get and not args.set:
        sys.exit("Specify either --get or --set.")

    if args.get:
        vals = get_param(args.confs, args.symbol, args.param)
        for conf, sym, val in vals:
            print(f"{conf} {sym} {args.param}={val}")
    elif args.set:
        all_changes = set_param(args.confs, args.symbol, args.param, args.value)
        for change in all_changes:
            print(change)

if __name__ == "__main__":
    main()
