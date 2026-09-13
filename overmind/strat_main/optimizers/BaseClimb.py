"""
Basic interface for a climbing algo.

"""


class BaseClimber:
    def from_json(self, json_dict):
        pass

    def to_json(self):
        pass

    # Maybe even get the best at
    def get_best_climbed_params(self):
        pass

    def get_best_score(self):
        pass

    def next_epoch_params(self):
        pass

    def eval_and_update(self, stats_for_epoch):
        pass
