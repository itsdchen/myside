"""
Set of helper fxns to help regression tasks.


"""
from scipy.stats.stats import pearsonr

import numpy as np


# Returns True if the signal passes this filter.

# one_sig_array is an np.array of signal values
# one_returns_array is an np.array of returns values
def sig_passes_imba_filter(one_sig_array, one_returns_array, med_std_limit, mean_std_limit, corr_limit, signed_corr_limit):

    pctiles = [0.1, 1, 10, 25, 50, 75, 90, 99, 99.9]
    sig_pctiles = np.percentile(one_sig_array, pctiles)

    # Turn it to dict, it's just easier to read.
    pctile_to_val = {}
    for idx, pct in enumerate(pctiles):
        pctile_to_val[pct] = sig_pctiles[idx]

    med = pctile_to_val[50]

    #med = np.median(this_sig_vals)
    mean = one_sig_array.mean()
    std = one_sig_array.std()

    # Just in cases. I'm not sure if I did this right for all versions of the
    # input things as 1- or 2- d. This is so stupid.
    corr = pearsonr(one_sig_array.ravel(), one_returns_array.ravel()).statistic

    blacklisted = False
    if std == 0:
        blacklisted = True

    # Median bias
    if abs(med/std) > med_std_limit:
        blacklisted = True

    # Mean bias
    if abs(mean/std) > mean_std_limit:
        blacklisted = True

    # Percentile bias
    if pctile_to_val[25] > 0 or pctile_to_val[75] < 0:
        blacklisted = True

    # Extreme values.
    if (abs(pctile_to_val[10]) > 3 * abs(pctile_to_val[90])) or (abs(pctile_to_val[10]) < 1/3 * abs(pctile_to_val[90])) :
        blacklisted = True

    if (abs(pctile_to_val[1]) > 3 * abs(pctile_to_val[99])) or (abs(pctile_to_val[1]) < 1/3 * abs(pctile_to_val[99])) :
        blacklisted = True

    # Turning this off for the ctl.
    # Extreme outlier filtering.
    # TODO: warn in a log that this is creating issues.
    #if max(one_sig_array) > 200 * pctile_to_val[99]:
    #    blacklisted = True
    #if min(one_sig_array) < 200 * pctile_to_val[1]:
    #    blacklisted = True

    # Consider removing this filter. If we do though, we'll want to go back
    # to the lasso or enet.
    # It's just that there are examples of isgnals that are 0 in a LOT
    # of places but take on meaningful values at the extremes, and those extremes
    # matter. So have a good way to deal with that in conjunction with
    # exploding coefficients.
    # Maybe the cleanest way to do things is to just enforce enet-ness.
    # IT's also
    #if pctile_to_val[99] > 250 * pctile_to_val[75]:
    ##    # TODO: warn in a log that this is creating issues.
    #    blacklisted = True
    #if pctile_to_val[1] < 250 * pctile_to_val[25]:
    #    blacklisted = True

    if abs(corr) < corr_limit:
        blacklisted = True

    if corr < signed_corr_limit:
        blacklisted = True

    return not blacklisted



# Let's add some stub fxns to help us deal with matrices and stuff.

def standardize_sig_mat():
    pass

# Given a matrix and a set of columns, get the subset for that.
