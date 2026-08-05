#!/usr/bin/env python3
"""
Plot QoE metrics (SSIM, stall, number of played segments) grouped by 
bottleneck bandwidth, number of clients, and mode.
Shows average values as bars with min/max intervals.
"""

import json
import os
from pathlib import Path
from collections import defaultdict
import matplotlib.pyplot as plt
import numpy as np

def load_experiment_data(experiments_dir="experiments"):
    """
    Load all experiment data from the experiments directory.
    Returns a list of dicts with structure:
    {
        'mode': str,
        'num_clients': int,
        'bandwidth': float,
        'client_id': str,
        'total_stall': float,
        'average_ssim': float,
        'num_segments': int
    }
    """
    data = []
    exp_path = Path(experiments_dir)
    
    if not exp_path.exists():
        print(f"Warning: {experiments_dir} directory not found")
        return data
    
    # Iterate through all experiment directories
    for exp_dir in sorted(exp_path.iterdir()):
        if not exp_dir.is_dir():
            continue
        
        # Load experiment config
        config_file = exp_dir / "experiment_config.json"
        if not config_file.exists():
            print(f"Skipping {exp_dir.name}: no experiment_config.json")
            continue
        
        try:
            with open(config_file) as f:
                config = json.load(f)
        except Exception as e:
            print(f"Error reading config from {exp_dir.name}: {e}")
            continue
        
        mode = config.get('mode_name', 'unknown')
        num_clients = config.get('num_clients', 0)
        bandwidth = config.get('bottleneck_bandwidth_mbit', 0)
        
        # Iterate through all client directories
        for client_dir in sorted(exp_dir.iterdir()):
            if not client_dir.is_dir() or not client_dir.name.startswith('client'):
                continue
            
            # Load QoE metrics
            qoe_file = client_dir / "qoe_metrics.json"
            if not qoe_file.exists():
                continue
            
            try:
                with open(qoe_file) as f:
                    qoe = json.load(f)
                
                summary = qoe.get('summary', {})
                per_segment = qoe.get('per_segment', [])
                
                data.append({
                    'mode': mode,
                    'num_clients': num_clients,
                    'bandwidth': bandwidth,
                    'client_id': client_dir.name,
                    'total_stall': summary.get('total_stall_seconds', 0),
                    'average_ssim': summary.get('average_ssim', 0),
                    'num_segments': len(per_segment)
                })
            
            except Exception as e:
                print(f"Error processing {client_dir}: {e}")
                continue
    
    return data


def group_and_aggregate(data):
    """
    Group data by (bandwidth, num_clients, mode) and calculate statistics.
    Returns dict with structure:
    {
        (bandwidth, num_clients, mode): {
            'total_stall': {'avg': float, 'min': float, 'max': float},
            'average_ssim': {'avg': float, 'min': float, 'max': float},
            'num_segments': {'avg': float, 'min': float, 'max': float}
        }
    }
    """
    grouped = defaultdict(lambda: {
        'total_stall': [],
        'average_ssim': [],
        'num_segments': []
    })
    
    for entry in data:
        key = (entry['bandwidth'], entry['num_clients'], entry['mode'])
        grouped[key]['total_stall'].append(entry['total_stall'])
        grouped[key]['average_ssim'].append(entry['average_ssim'])
        grouped[key]['num_segments'].append(entry['num_segments'])
    
    # Calculate statistics
    stats = {}
    for key, values in grouped.items():
        stats[key] = {}
        for metric in ['total_stall', 'average_ssim', 'num_segments']:
            vals = values[metric]
            if vals:
                stats[key][metric] = {
                    'avg': np.mean(vals),
                    'min': np.min(vals),
                    'max': np.max(vals),
                    'count': len(vals)
                }
            else:
                stats[key][metric] = {
                    'avg': 0,
                    'min': 0,
                    'max': 0,
                    'count': 0
                }
    
    return stats


def plot_metrics(stats, output_dir="plots"):
    """
    Create plots for each metric grouped by bandwidth, num_clients, and mode.
    """
    os.makedirs(output_dir, exist_ok=True)
    
    if not stats:
        print("No data to plot")
        return
    
    # Extract unique values for grouping
    bandwidths = sorted(set(k[0] for k in stats.keys()))
    num_clients_list = sorted(set(k[1] for k in stats.keys()))
    modes = sorted(set(k[2] for k in stats.keys()))
    
    print(f"Found data for:")
    print(f"  Bandwidths: {bandwidths} Mbit/s")
    print(f"  Number of clients: {num_clients_list}")
    print(f"  Modes: {modes}")
    
    metrics_info = [
        ('total_stall', 'Total Stall Duration', 'seconds'),
        ('average_ssim', 'Average SSIM', 'score'),
        ('num_segments', 'Number of Played Segments', 'count')
    ]
    
    for metric, title, unit in metrics_info:
        # Create a figure with subplots for each bandwidth
        n_bandwidth = len(bandwidths)
        fig, axes = plt.subplots(1, n_bandwidth, figsize=(6*n_bandwidth, 6), squeeze=False)
        axes = axes.flatten()
        
        for bw_idx, bandwidth in enumerate(bandwidths):
            ax = axes[bw_idx]
            
            # Prepare data for this bandwidth
            x_labels = []
            avg_values = []
            min_values = []
            max_values = []
            colors = []
            
            mode_colors = {'l4s': 'blue', 'classic': 'orange', 'dualpi2': 'green'}
            
            x_pos = 0
            x_ticks = []
            x_tick_labels = []
            
            for num_clients in num_clients_list:
                group_start = x_pos
                for mode in modes:
                    key = (bandwidth, num_clients, mode)
                    if key in stats:
                        stat = stats[key][metric]
                        avg = stat['avg']
                        min_val = stat['min']
                        max_val = stat['max']
                        
                        avg_values.append(avg)
                        min_values.append(avg - min_val)
                        max_values.append(max_val - avg)
                        x_labels.append(f"{mode}\n({num_clients})")
                        colors.append(mode_colors.get(mode, 'gray'))
                        x_ticks.append(x_pos)
                        x_pos += 1
                    else:
                        # Add placeholder for missing data
                        avg_values.append(0)
                        min_values.append(0)
                        max_values.append(0)
                        x_labels.append(f"{mode}\n({num_clients})")
                        colors.append('lightgray')
                        x_ticks.append(x_pos)
                        x_pos += 1
                
                # Add spacing between client groups
                x_pos += 0.5
                
                # Mark client group center
                group_end = x_pos - 0.5
                group_center = (group_start + group_end - len(modes) + 1) / 2 + (len(modes) - 1) / 2
                x_tick_labels.append((group_center, f"{num_clients} clients"))
            
            # Create bar plot
            bars = ax.bar(x_ticks, avg_values, color=colors, alpha=0.7, edgecolor='black')
            
            # Add error bars (min/max intervals)
            ax.errorbar(x_ticks, avg_values, 
                       yerr=[min_values, max_values],
                       fmt='none', ecolor='black', capsize=5, capthick=2)
            
            # Formatting
            ax.set_xlabel('Mode (Number of Clients)', fontsize=12, fontweight='bold')
            ax.set_ylabel(f'{title} ({unit})', fontsize=12, fontweight='bold')
            ax.set_title(f'Bandwidth: {bandwidth} Mbit/s', fontsize=14, fontweight='bold')
            ax.set_xticks(x_ticks)
            ax.set_xticklabels(x_labels, rotation=45, ha='right')
            ax.grid(axis='y', alpha=0.3)
            
            # Add value labels on bars
            for i, (bar, avg) in enumerate(zip(bars, avg_values)):
                if avg > 0:
                    height = bar.get_height()
                    ax.text(bar.get_x() + bar.get_width()/2., height,
                           f'{avg:.2f}',
                           ha='center', va='bottom', fontsize=8)
        
        plt.tight_layout()
        output_file = os.path.join(output_dir, f'{metric}_by_bandwidth_clients_mode.png')
        plt.savefig(output_file, dpi=300, bbox_inches='tight')
        print(f"Saved plot: {output_file}")
        plt.close()
    
    # Create a combined overview plot
    fig, axes = plt.subplots(len(metrics_info), 1, figsize=(14, 6*len(metrics_info)))
    if len(metrics_info) == 1:
        axes = [axes]
    
    for metric_idx, (metric, title, unit) in enumerate(metrics_info):
        ax = axes[metric_idx]
        
        # Create grouped bar chart
        x_labels = []
        mode_data = {mode: {'avg': [], 'yerr_lower': [], 'yerr_upper': []} for mode in modes}
        
        x_pos = 0
        x_ticks = []
        
        for bandwidth in bandwidths:
            for num_clients in num_clients_list:
                label = f"{bandwidth}Mb\n{num_clients}c"
                x_labels.append(label)
                x_ticks.append(x_pos)
                
                for mode in modes:
                    key = (bandwidth, num_clients, mode)
                    if key in stats:
                        stat = stats[key][metric]
                        mode_data[mode]['avg'].append(stat['avg'])
                        mode_data[mode]['yerr_lower'].append(stat['avg'] - stat['min'])
                        mode_data[mode]['yerr_upper'].append(stat['max'] - stat['avg'])
                    else:
                        mode_data[mode]['avg'].append(0)
                        mode_data[mode]['yerr_lower'].append(0)
                        mode_data[mode]['yerr_upper'].append(0)
                
                x_pos += 1
        
        # Plot bars for each mode
        bar_width = 0.25
        mode_colors = {'l4s': 'blue', 'classic': 'orange', 'dualpi2': 'green'}
        
        for mode_idx, mode in enumerate(modes):
            offset = (mode_idx - len(modes)/2 + 0.5) * bar_width
            positions = [x + offset for x in x_ticks]
            
            ax.bar(positions, mode_data[mode]['avg'], bar_width,
                  label=mode, color=mode_colors.get(mode, 'gray'),
                  alpha=0.7, edgecolor='black')
            
            ax.errorbar(positions, mode_data[mode]['avg'],
                       yerr=[mode_data[mode]['yerr_lower'], mode_data[mode]['yerr_upper']],
                       fmt='none', ecolor='black', capsize=3, capthick=1)
        
        ax.set_xlabel('Bandwidth / Number of Clients', fontsize=12, fontweight='bold')
        ax.set_ylabel(f'{title} ({unit})', fontsize=12, fontweight='bold')
        ax.set_title(f'{title} Comparison', fontsize=14, fontweight='bold')
        ax.set_xticks(x_ticks)
        ax.set_xticklabels(x_labels, rotation=45, ha='right')
        ax.legend()
        ax.grid(axis='y', alpha=0.3)
    
    plt.tight_layout()
    output_file = os.path.join(output_dir, 'combined_metrics_overview.png')
    plt.savefig(output_file, dpi=300, bbox_inches='tight')
    print(f"Saved combined plot: {output_file}")
    plt.close()


def print_summary_table(stats):
    """Print a summary table of the data."""
    print("\n" + "="*100)
    print("SUMMARY TABLE")
    print("="*100)
    print(f"{'Bandwidth':>10} {'Clients':>8} {'Mode':>10} {'Stall(s)':>15} {'SSIM':>15} {'Segments':>15}")
    print(f"{'(Mbit/s)':>10} {'':>8} {'':>10} {'Avg[Min-Max]':>15} {'Avg[Min-Max]':>15} {'Avg[Min-Max]':>15}")
    print("-"*100)
    
    for key in sorted(stats.keys()):
        bandwidth, num_clients, mode = key
        stall = stats[key]['total_stall']
        ssim = stats[key]['average_ssim']
        segs = stats[key]['num_segments']
        
        print(f"{bandwidth:>10.2f} {num_clients:>8} {mode:>10} "
              f"{stall['avg']:>6.2f}[{stall['min']:>5.2f}-{stall['max']:>5.2f}] "
              f"{ssim['avg']:>6.4f}[{ssim['min']:>5.4f}-{ssim['max']:>5.4f}] "
              f"{segs['avg']:>6.1f}[{segs['min']:>3.0f}-{segs['max']:>3.0f}]")
    print("="*100 + "\n")


def main():
    print("Loading experiment data...")
    data = load_experiment_data()
    
    if not data:
        print("No data found. Make sure experiments directory exists with valid data.")
        return
    
    print(f"Loaded {len(data)} client results from experiments")
    
    print("\nGrouping and aggregating data...")
    stats = group_and_aggregate(data)
    
    print(f"Grouped into {len(stats)} unique configurations")
    
    print_summary_table(stats)
    
    print("\nGenerating plots...")
    plot_metrics(stats)
    
    print("\nDone!")


if __name__ == "__main__":
    main()
