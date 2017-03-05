#!/usr/bin/env python

from collections import defaultdict
import h5py
import numpy as np
import matplotlib.pyplot as plt
import os, os.path as osp
import seaborn as sns
from pandas import DataFrame

from sandbox.sandy.adversarial.io_util import get_all_result_paths
from sandbox.sandy.adversarial.plot_seaborn import NORMS

#RESULT_DIRS = ["/home/shhuang/src/rllab-private/sandbox/sandy/adversarial/output_sleeper_Feb19", \
#               "/home/shhuang/src/rllab-private/sandbox/sandy/adversarial/output_sleeper_Feb20"]
#RESULT_DIRS = ["/home/shhuang/src/rllab-private/sandbox/sandy/adversarial/output_sleeper_dualdescent_Feb22"]
RESULT_DIRS = ["/home/shhuang/src/rllab-private/data/s3/adv-sleeper-rollouts/exp015"]
EXP_TO_DESC = dict(exp037="A3C LSTM (4 frames)", exp038="A3C LSTM (1 frame)")
SHOW_PLOT = False
PREFIXES = dict(none='None, ' + r'$\epsilon$' + ' = ', \
                only='Only others, ' + r'$\epsilon$' + ' = ', \
                also='Also others, ' + r'$\epsilon$' + ' = ')

def update_sleeper_results(d, actions_orig, actions_adv):
    # d = dictionary to update; sublevel of full sleeper_results dictionary
    # actions_orig = list of actions taken when there is no perturbation
    # actions_adv = list of actions taken when there is a perturbation
    for key in ['success', 'none', 'only_others', 'also_others', 'total']:
        if key not in d:
            d[key] = 0

    diff_actions = (actions_orig != actions_adv)
    if sum(diff_actions) == 1 and sum(diff_actions[:-1]) == 0:
        d['success'] += 1
    elif sum(diff_actions) == 0:
        d['none'] += 1
    elif sum(diff_actions) > 0 and not diff_actions[-1]:
        d['only_others'] += 1
    elif sum(diff_actions) > 0 and diff_actions[-1]:
        d['also_others'] += 1
    d['total'] += 1

def turn_into_ratios(sleeper_results):
    for k1 in sleeper_results:
        for k2 in sleeper_results[k1]:
            g = sleeper_results[k1][k2]
            assert g['total'] == g['success'] + g['none'] + g['also_others'] + g['only_others']
            for k3 in ['success', 'none', 'also_others', 'only_others']:
                g[k3] = float(g[k3]) / g['total']

def get_sleeper_results(base_result_dirs):
    # Separates data by game+norm+experiment
    sleeper_results = {}
    for base_result_dir in base_result_dirs:
        result_dirs = os.listdir(base_result_dir)
        for result_dir in result_dirs:
            if not osp.isdir(osp.join(base_result_dir, result_dir)):  # Skip files
                continue
            # Round-about way of getting game and experiment name
            result_fnames = [x for x in os.listdir(osp.join(base_result_dir, result_dir)) \
                             if x.endswith('h5') and x.find('allvariants') < 0]
            for result_fname in result_fnames:
                exp_info = result_fname.split('_')
                eps = exp_info[2].replace('-','.',1)
                exp = exp_info[3]
                game = exp_info[-1].split('.')[0]

                if game == "command":
                    game = "chopper-command"
                elif game == "invaders":
                    game = "space-invaders"

                result_f = h5py.File(osp.join(base_result_dir, result_dir, result_fname), 'r')
                norm = result_f['adv_params']['norm'][()]

                key = ";".join([game, norm, exp])
                if key not in sleeper_results:
                    sleeper_results[key] = {}

                for i in range(len(result_f['rollouts'])):
                    g = result_f['rollouts'][str(i)]
                    k = g['k'][()]
                    key2 = ";".join([str(k), eps])
                    if key2 not in sleeper_results[key]:
                        sleeper_results[key][key2] = {}
                    actions_orig = np.argmax(g['action_prob_orig'][()], axis=1)
                    actions_adv = np.argmax(g['action_prob_adv'][()], axis=1)
                    update_sleeper_results(sleeper_results[key][key2], actions_orig, actions_adv)
                result_f.close()

    turn_into_ratios(sleeper_results)
    return sleeper_results

def plot_sleeper_results(base_result_dirs):
    # Gather all the data to plot
    sleeper_data = get_sleeper_results(base_result_dirs)

    for key1 in sleeper_data:
        game, norm, exp = key1.split(';')
        sleeper_df_dict = defaultdict(list)
        all_eps = set()
        for key2 in sleeper_data[key1]:
            k, eps = key2.split(';')
            sleeper_df_dict['game'].append(game)
            sleeper_df_dict['norm'].append(norm)
            sleeper_df_dict['exp'].append(exp)
            sleeper_df_dict['k'].append(k)
            sleeper_df_dict[r'$\epsilon$'].append(eps)
            all_eps.add(eps)
            sleeper_df_dict['eps_none'].append(PREFIXES['none'] + eps)
            sleeper_df_dict['eps_only-others'].append(PREFIXES['only'] + eps)
            sleeper_df_dict['eps_also-others'].append(PREFIXES['also'] + eps)
            for key3 in ['success', 'none', 'also_others', 'only_others', 'total']:
                sleeper_df_dict[key3].append(sleeper_data[key1][key2][key3])
            sleeper_df_dict['none+only_others+also_others'].append(\
                    sleeper_data[key1][key2]['none'] + \
                    sleeper_data[key1][key2]['only_others'] + \
                    sleeper_data[key1][key2]['also_others'])
            sleeper_df_dict['only_others+also_others'].append(
                    sleeper_data[key1][key2]['only_others'] + \
                    sleeper_data[key1][key2]['also_others'])
            sleeper_df_dict['none+only_others'].append(
                    sleeper_data[key1][key2]['none'] + \
                    sleeper_data[key1][key2]['only_others'])

        eps_order = [(x, float(x)) for x in all_eps]
        eps_order = sorted(eps_order, key=lambda x: x[1])
        eps_order = [x[0] for x in eps_order]

        # Create DataFrame
        sleeper_df = DataFrame(sleeper_df_dict)

        # Plot success rate
        sns.set_palette(sns.color_palette('muted'))
        ax = sns.barplot(x="k", y="success", hue=r'$\epsilon$', data=sleeper_df, \
                         hue_order=eps_order)
        ax.set(ylabel='success rate')
        game_cap = ' '.join([word.capitalize() for word in game.split('-')])
        fig_title = game_cap + ", " + EXP_TO_DESC[exp] + ", " + NORMS[norm] + ' norm'
        sns.plt.title(fig_title)
        if SHOW_PLOT:
            sns.plt.show()
        extension = '.png'
        ax.get_figure().savefig(osp.join(base_result_dirs[0], game + '_' + exp + '_' + norm + extension), bbox_inches='tight', pad_inches=0.0)
        sns.plt.clf()

        # Plot rate of failure modes
        sns.set_palette(sns.color_palette('deep'))
        ax = sns.barplot(x="k", y="none+only_others+also_others", \
                         hue="eps_also-others", data=sleeper_df, \
                         hue_order=[PREFIXES['also']+x for x in eps_order])
        sns.set_palette(sns.color_palette('muted'))
        ax = sns.barplot(x="k", y="none+only_others", \
                         hue="eps_only-others", data=sleeper_df, \
                         hue_order=[PREFIXES['only']+x for x in eps_order])
        sns.set_palette(sns.color_palette('pastel'))
        ax = sns.barplot(x="k", y="none", hue="eps_none", data=sleeper_df, \
                         hue_order=[PREFIXES['none']+x for x in eps_order])
        #ax.legend_.remove()
        ax.set(ylabel='failure rates')
        plt.legend(bbox_to_anchor=(1, 1), loc=2, borderaxespad=0.)

        # ax.legend(ncol=2, loc="lower right", frameon=True)
        game_cap = ' '.join([word.capitalize() for word in game.split('-')])
        fig_title = game_cap + ", " + EXP_TO_DESC[exp] + ", " + NORMS[norm] + ' norm: Failure Modes'
        sns.plt.title(fig_title)
        if SHOW_PLOT:
            sns.plt.show()
        ax.get_figure().savefig(osp.join(base_result_dirs[0], game + '_' + exp + '_' + norm + '_failure' + extension), bbox_inches='tight', pad_inches=0.0)
        sns.plt.clf()

def main():
    plot_sleeper_results(RESULT_DIRS)

if __name__ == "__main__":
    main()
