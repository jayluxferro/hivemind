"""Generate all paper figures from benchmark results."""

import json
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# Style
plt.rcParams.update({
    'font.size': 9,
    'font.family': 'serif',
    'axes.labelsize': 10,
    'axes.titlesize': 10,
    'xtick.labelsize': 8,
    'ytick.labelsize': 8,
    'legend.fontsize': 8,
    'figure.dpi': 300,
})

import os
_dir = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_dir, '..', 'evaluation', 'results.json')) as f:
    data = json.load(f)


# ============================================================
# Figure 2: Failure rate bar chart (direct vs HiveMind)
# ============================================================
def fig_failure_rates():
    scenarios = []
    direct_rates = []
    hm_rates = []

    # Use first occurrence of each scenario
    seen = set()
    for c in data['comparisons']:
        name = c['scenario']
        if name in seen:
            continue
        seen.add(name)
        scenarios.append(name)
        direct_rates.append(c['direct']['failure_rate'])
        hm_rates.append(c['hivemind']['failure_rate'])

    x = np.arange(len(scenarios))
    width = 0.35

    fig, ax = plt.subplots(figsize=(4.5, 2.5))
    bars1 = ax.bar(x - width/2, direct_rates, width, label='Direct',
                   color='#d62728', alpha=0.85)
    bars2 = ax.bar(x + width/2, hm_rates, width, label='HiveMind',
                   color='#2ca02c', alpha=0.85)

    ax.set_ylabel('Failure Rate (%)')
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=30, ha='right')
    ax.legend(loc='upper left')
    ax.set_ylim(0, 110)
    ax.axhline(y=0, color='gray', linewidth=0.5)

    # Add value labels on bars
    for bar in bars1:
        h = bar.get_height()
        if h > 0:
            ax.annotate(f'{h:.0f}%', xy=(bar.get_x() + bar.get_width()/2, h),
                       xytext=(0, 2), textcoords='offset points',
                       ha='center', va='bottom', fontsize=6)
    for bar in bars2:
        h = bar.get_height()
        if h > 0:
            ax.annotate(f'{h:.0f}%', xy=(bar.get_x() + bar.get_width()/2, h),
                       xytext=(0, 2), textcoords='offset points',
                       ha='center', va='bottom', fontsize=6)

    fig.tight_layout()
    fig.savefig('fig_failure_rates.pdf', bbox_inches='tight')
    plt.close(fig)
    print('Generated fig_failure_rates.pdf')


# ============================================================
# Figure 3: Throughput (completed tasks/min) vs agent count
# ============================================================
def fig_throughput():
    micro = {}
    for c in data['comparisons']:
        name = c['scenario']
        if name.startswith('micro-'):
            n = int(name.split('-')[1])
            micro[n] = c

    agents = sorted(micro.keys())
    direct_tp = [micro[n]['direct']['throughput_tasks_per_min'] for n in agents]
    hm_tp = [micro[n]['hivemind']['throughput_tasks_per_min'] for n in agents]
    # Also compute completed agents/min as alternative
    direct_completed = [micro[n]['direct']['agents_alive'] for n in agents]
    hm_completed = [micro[n]['hivemind']['agents_alive'] for n in agents]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(4.5, 2.2))

    # Left: completed agents
    ax1.plot(agents, direct_completed, 'o-', color='#d62728', label='Direct',
             markersize=4, linewidth=1.5)
    ax1.plot(agents, hm_completed, 's-', color='#2ca02c', label='HiveMind',
             markersize=4, linewidth=1.5)
    ax1.set_xlabel('Concurrent Agents')
    ax1.set_ylabel('Agents Completed')
    ax1.legend(fontsize=7)
    ax1.set_ylim(-1, 55)

    # Right: throughput
    ax2.plot(agents, direct_tp, 'o-', color='#d62728', label='Direct',
             markersize=4, linewidth=1.5)
    ax2.plot(agents, hm_tp, 's-', color='#2ca02c', label='HiveMind',
             markersize=4, linewidth=1.5)
    ax2.set_xlabel('Concurrent Agents')
    ax2.set_ylabel('Tasks/min')
    ax2.legend(fontsize=7)

    fig.tight_layout()
    fig.savefig('fig_throughput.pdf', bbox_inches='tight')
    plt.close(fig)
    print('Generated fig_throughput.pdf')


# ============================================================
# Figure 4: Ablation bar chart
# ============================================================
def fig_ablation():
    configs = ['Full HiveMind']
    fail_rates = [0.0]

    # Get ablation results
    for a in data['ablations']:
        name = a['scenario'].replace('ablation-', '').replace('-', ' ').title()
        configs.append(name)
        fail_rates.append(a['failure_rate'])

    colors = ['#2ca02c'] + ['#1f77b4'] * (len(configs) - 3) + ['#d62728', '#d62728']

    fig, ax = plt.subplots(figsize=(4.5, 2.2))
    bars = ax.barh(range(len(configs)), fail_rates, color=colors, alpha=0.85)
    ax.set_yticks(range(len(configs)))
    ax.set_yticklabels(configs, fontsize=7)
    ax.set_xlabel('Failure Rate (%)')
    ax.set_xlim(0, 100)
    ax.invert_yaxis()

    for bar, rate in zip(bars, fail_rates):
        if rate > 0:
            ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height()/2,
                   f'{rate:.1f}%', va='center', fontsize=7)

    fig.tight_layout()
    fig.savefig('fig_ablation.pdf', bbox_inches='tight')
    plt.close(fig)
    print('Generated fig_ablation.pdf')


# ============================================================
# Figure 5: Token waste comparison
# ============================================================
def fig_token_waste():
    scenarios = []
    direct_waste = []
    hm_waste = []

    seen = set()
    for c in data['comparisons']:
        name = c['scenario']
        if name in seen:
            continue
        seen.add(name)
        scenarios.append(name)
        direct_waste.append(c['direct']['wasted_tokens'])
        hm_waste.append(c['hivemind']['wasted_tokens'])

    x = np.arange(len(scenarios))
    width = 0.35

    fig, ax = plt.subplots(figsize=(4.5, 2.2))
    ax.bar(x - width/2, [w/1000 for w in direct_waste], width,
           label='Direct', color='#d62728', alpha=0.85)
    ax.bar(x + width/2, [w/1000 for w in hm_waste], width,
           label='HiveMind', color='#2ca02c', alpha=0.85)

    ax.set_ylabel('Wasted Tokens (K)')
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios, rotation=30, ha='right')
    ax.legend(loc='upper left')

    fig.tight_layout()
    fig.savefig('fig_token_waste.pdf', bbox_inches='tight')
    plt.close(fig)
    print('Generated fig_token_waste.pdf')


# ============================================================
# Figure: Cost analysis
# ============================================================
def fig_cost_analysis():
    """Dollar cost of wasted tokens at different pricing tiers."""
    # Anthropic pricing (per million tokens, input)
    pricing = {
        'Haiku': {'input': 0.80, 'output': 4.00},
        'Sonnet': {'input': 3.00, 'output': 15.00},
        'Opus': {'input': 15.00, 'output': 75.00},
    }

    # Get total waste from all scenarios (direct mode)
    total_waste_direct = sum(c['direct']['wasted_tokens']
                            for i, c in enumerate(data['comparisons'])
                            if i < 7)  # first 7 unique
    total_waste_hm = sum(c['hivemind']['wasted_tokens']
                         for i, c in enumerate(data['comparisons'])
                         if i < 7)

    # Scale to realistic 1-day workload: 10 runs of the full suite
    scale = 10
    direct_tokens = total_waste_direct * scale
    hm_tokens = total_waste_hm * scale

    models = list(pricing.keys())
    direct_costs = [direct_tokens * pricing[m]['input'] / 1e6 for m in models]
    hm_costs = [hm_tokens * pricing[m]['input'] / 1e6 for m in models]

    x = np.arange(len(models))
    width = 0.35

    fig, ax = plt.subplots(figsize=(3.2, 2.2))
    ax.bar(x - width/2, direct_costs, width, label='Direct',
           color='#d62728', alpha=0.85)
    ax.bar(x + width/2, hm_costs, width, label='HiveMind',
           color='#2ca02c', alpha=0.85)

    ax.set_ylabel('Wasted Cost (USD/day)')
    ax.set_xticks(x)
    ax.set_xticklabels(models)
    ax.legend(fontsize=7)

    for i, (d, h) in enumerate(zip(direct_costs, hm_costs)):
        if d > 0:
            ax.annotate(f'${d:.2f}', xy=(i - width/2, d),
                       xytext=(0, 2), textcoords='offset points',
                       ha='center', va='bottom', fontsize=6)
        if h > 0:
            ax.annotate(f'${h:.2f}', xy=(i + width/2, h),
                       xytext=(0, 2), textcoords='offset points',
                       ha='center', va='bottom', fontsize=6)

    fig.tight_layout()
    fig.savefig('fig_cost.pdf', bbox_inches='tight')
    plt.close(fig)
    print('Generated fig_cost.pdf')


if __name__ == '__main__':
    fig_failure_rates()
    fig_throughput()
    fig_ablation()
    fig_token_waste()
    fig_cost_analysis()
    print('All figures generated.')
