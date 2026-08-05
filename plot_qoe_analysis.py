#!/usr/bin/env python3
"""
Plot QoE metrics (total stall and SSIM) grouped by bitrate, number of clients, and mode.
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
        'client_id': str,
        'total_stall': float,
        'average_ssim': float,
        'average_bitrate': float
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
        
        with open(config_file) as f:
            config = json.load(f)
        
        mode = config.get('mode_name', 'unknown')
        num_clients = config.get('num_clients', 0)
        
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
                
                # Calculate average bitrate from segments
                if per_segment:
                    bitrates = [seg.get('bandwidth', 0) for seg in per_segment if seg.get('bandwidth')]
                    avg_bitrate = np.mean(bitrates) / 1e6 if bitrates else 0  # Convert to Mbps
                else:
                    avg_bitrate = 0
                
                data.append({
                    'mode': mode,
                    'num_clients': num_clients,
                    'client_id': client_dir.name,
                    'total_stall': summary.get('total_stall_seconds', 0),
                    'average_ssim': summary.get('average_ssim', 0),
                    'average_bitrate': avg_bitrate
                })
            
            except Exception as e:
                print(f"Error processing {client_dir}: {e}")
                continue
    
    return data


def group_data_by_bitrate_bins(data, bins=[0, 2, 4, 6, 8, 10]):
    """
    Group data by bitrate bins, number of clients, and mode.
    Returns nested dict structure.
    """
    grouped = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    
    for entry in data:
        bitrate = entry['average_bitrate']
        # Find appropriate bin
        bin_label = None
        for i in range(len(bins) - 1):
            if bins[i] <= bitrate < bins[i + 1]:
                bin_label = f"{bins[i]}-{bins[i+1]} Mbps"
                break
        if bitrate >= bins[-1]:
            bin_label = f"{bins[-1]}+ Mbps"
        
        if bin_label:
            grouped[entry['num_clients']][entry['mode']][bin_label].append(entry)
    
    return grouped


def calculate_statistics(entries, metric_key):
    """Calculate mean, min, max for a given metric."""
    values = [e[metric_key] for e in entries]
    if not values:
        return 0, 0, 0
    return np.mean(values), np.min(values), np.max(values)


def plot_metrics_by_bitrate(data, output_dir="plots"):
    """
    Create plots showing total stall and SSIM grouped by bitrate, num_clients, and mode.
    """
    # Create output directory
    Path(output_dir).mkdir(exist_ok=True)
    
    # Define bitrate bins
    bitrate_bins = [0, 2, 4, 6, 8, 10]
    
    # Group data
    grouped = group_data_by_bitrate_bins(data, bitrate_bins)
    
    if not grouped:
        print("No data to plot!")
        return
    
    # Get unique values
    num_clients_list = sorted(grouped.keys())
    modes = set()
    bitrate_labels = set()
    
    for nc in num_clients_list:
        for mode in grouped[nc].keys():
            modes.add(mode)
            for br_label in grouped[nc][mode].keys():
                bitrate_labels.add(br_label)
    
    modes = sorted(modes)
    bitrate_labels = sorted(bitrate_labels)
    
    # Create plots for each number of clients
    for num_clients in num_clients_list:
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 6))
        fig.suptitle(f'QoE Metrics - {num_clients} Clients', fontsize=16, fontweight='bold')
        
        # Prepare data for plotting
        x_pos = np.arange(len(bitrate_labels))
        width = 0.35
        
        for metric_idx, (ax, metric_key, metric_name, ylabel) in enumerate([
            (ax1, 'total_stall', 'Total Stall Duration', 'Stall (seconds)'),
            (ax2, 'average_ssim', 'Average SSIM', 'SSIM Score')
        ]):
            mode_offset = {modes[i]: (i - len(modes)/2 + 0.5) * width for i in range(len(modes))}
            
            for mode in modes:
                means = []
                mins = []
                maxs = []
                
                for br_label in bitrate_labels:
                    entries = grouped[num_clients][mode].get(br_label, [])
                    if entries:
                        mean, min_val, max_val = calculate_statistics(entries, metric_key)
                        means.append(mean)
                        mins.append(min_val)
                        maxs.append(max_val)
                    else:
                        means.append(0)
                        mins.append(0)
                        maxs.append(0)
                
                # Calculate error bars (distance from mean to min/max)
                yerr_lower = [means[i] - mins[i] for i in range(len(means))]
                yerr_upper = [maxs[i] - means[i] for i in range(len(means))]
                
                # Plot bars with error bars
                bars = ax.bar(
                    x_pos + mode_offset[mode],
                    means,
                    width,
                    label=mode.capitalize(),
                    alpha=0.8,
                    capsize=5
                )
                
                # Add error bars
                ax.errorbar(
                    x_pos + mode_offset[mode],
                    means,
                    yerr=[yerr_lower, yerr_upper],
                    fmt='none',
                    ecolor='black',
                    capsize=5,
                    capthick=1,
                    alpha=0.7
                )
                
                # Add value labels on bars
                for i, (bar, mean) in enumerate(zip(bars, means)):
                    height = bar.get_height()
                    if height > 0:
                        ax.text(
                            bar.get_x() + bar.get_width() / 2,
                            height,
                            f'{mean:.2f}',
                            ha='center',
                            va='bottom',
                            fontsize=8
                        )
            
            ax.set_xlabel('Average Bitrate', fontsize=12, fontweight='bold')
            ax.set_ylabel(ylabel, fontsize=12, fontweight='bold')
            ax.set_title(metric_name, fontsize=14)
            ax.set_xticks(x_pos)
            ax.set_xticklabels(bitrate_labels, rotation=45, ha='right')
            ax.legend()
            ax.grid(axis='y', alpha=0.3)
        
        plt.tight_layout()
        output_file = Path(output_dir) / f"qoe_metrics_{num_clients}_clients.png"
        plt.savefig(output_file, dpi=300, bbox_inches='tight')
        print(f"Saved plot: {output_file}")
        plt.close()


def plot_combined_overview(data, output_dir="plots"):
    """
    Create overview plots showing all metrics across all client counts.
    """
    Path(output_dir).mkdir(exist_ok=True)
    
    # Group by num_clients and mode
    grouped = defaultdict(lambda: defaultdict(list))
    
    for entry in data:
        grouped[entry['num_clients']][entry['mode']].append(entry)
    
    if not grouped:
        print("No data to plot!")
        return
    
    num_clients_list = sorted(grouped.keys())
    modes = set()
    for nc in num_clients_list:
        modes.update(grouped[nc].keys())
    modes = sorted(modes)
    
    # Create combined overview plot
    fig, axes = plt.subplots(2, 2, figsize=(16, 12))
    fig.suptitle('QoE Metrics Overview - All Configurations', fontsize=16, fontweight='bold')
    
    for metric_idx, (metric_key, metric_name) in enumerate([
        ('total_stall', 'Total Stall Duration (s)'),
        ('average_ssim', 'Average SSIM Score'),
    ]):
        for plot_type_idx, plot_type in enumerate(['bar', 'line']):
            ax = axes[metric_idx][plot_type_idx]
            
            x_pos = np.arange(len(num_clients_list))
            width = 0.35
            
            for mode_idx, mode in enumerate(modes):
                means = []
                mins = []
                maxs = []
                
                for nc in num_clients_list:
                    entries = grouped[nc].get(mode, [])
                    if entries:
                        mean, min_val, max_val = calculate_statistics(entries, metric_key)
                        means.append(mean)
                        mins.append(min_val)
                        maxs.append(max_val)
                    else:
                        means.append(0)
                        mins.append(0)
                        maxs.append(0)
                
                if plot_type == 'bar':
                    yerr_lower = [means[i] - mins[i] for i in range(len(means))]
                    yerr_upper = [maxs[i] - means[i] for i in range(len(means))]
                    
                    offset = (mode_idx - len(modes)/2 + 0.5) * width
                    bars = ax.bar(
                        x_pos + offset,
                        means,
                        width,
                        label=mode.capitalize(),
                        alpha=0.8
                    )
                    ax.errorbar(
                        x_pos + offset,
                        means,
                        yerr=[yerr_lower, yerr_upper],
                        fmt='none',
                        ecolor='black',
                        capsize=5,
                        capthick=1,
                        alpha=0.7
                    )
                else:  # line plot
                    ax.plot(num_clients_list, means, marker='o', label=mode.capitalize(), linewidth=2)
                    ax.fill_between(num_clients_list, mins, maxs, alpha=0.2)
            
            if plot_type == 'bar':
                ax.set_xticks(x_pos)
                ax.set_xticklabels([f"{nc}" for nc in num_clients_list])
            
            ax.set_xlabel('Number of Clients', fontsize=11, fontweight='bold')
            ax.set_ylabel(metric_name, fontsize=11, fontweight='bold')
            ax.set_title(f"{metric_name} ({'Bar' if plot_type == 'bar' else 'Line'} Plot)", fontsize=12)
            ax.legend()
            ax.grid(alpha=0.3)
    
    plt.tight_layout()
    output_file = Path(output_dir) / "qoe_metrics_overview.png"
    plt.savefig(output_file, dpi=300, bbox_inches='tight')
    print(f"Saved overview plot: {output_file}")
    plt.close()


def print_statistics_summary(data):
    """Print a text summary of the statistics."""
    print("\n" + "="*80)
    print("QoE METRICS STATISTICS SUMMARY")
    print("="*80)
    
    # Group by num_clients and mode
    grouped = defaultdict(lambda: defaultdict(list))
    for entry in data:
        grouped[entry['num_clients']][entry['mode']].append(entry)
    
    for num_clients in sorted(grouped.keys()):
        print(f"\n{'─'*80}")
        print(f"NUMBER OF CLIENTS: {num_clients}")
        print(f"{'─'*80}")
        
        for mode in sorted(grouped[num_clients].keys()):
            entries = grouped[num_clients][mode]
            print(f"\n  Mode: {mode.upper()}")
            print(f"  {'-'*76}")
            
            # Total Stall Statistics
            stall_mean, stall_min, stall_max = calculate_statistics(entries, 'total_stall')
            print(f"    Total Stall Duration:")
            print(f"      Average: {stall_mean:.3f} s  |  Min: {stall_min:.3f} s  |  Max: {stall_max:.3f} s")
            
            # SSIM Statistics
            ssim_mean, ssim_min, ssim_max = calculate_statistics(entries, 'average_ssim')
            print(f"    Average SSIM:")
            print(f"      Average: {ssim_mean:.4f}  |  Min: {ssim_min:.4f}  |  Max: {ssim_max:.4f}")
            
            # Bitrate Statistics
            bitrate_mean, bitrate_min, bitrate_max = calculate_statistics(entries, 'average_bitrate')
            print(f"    Average Bitrate:")
            print(f"      Average: {bitrate_mean:.2f} Mbps  |  Min: {bitrate_min:.2f} Mbps  |  Max: {bitrate_max:.2f} Mbps")
            
            print(f"    Sample Size: {len(entries)} clients")
    
    print("\n" + "="*80 + "\n")


def main():
    """Main execution function."""
    print("Loading experiment data...")
    data = load_experiment_data("experiments")
    
    if not data:
        print("No data found! Make sure the experiments directory contains valid data.")
        return
    
    print(f"Loaded {len(data)} client measurements from experiments")
    
    # Print statistics summary
    print_statistics_summary(data)
    
    # Create plots
    print("\nGenerating plots...")
    plot_metrics_by_bitrate(data, output_dir="plots")
    plot_combined_overview(data, output_dir="plots")
    
    print("\nDone! Check the 'plots' directory for generated figures.")


if __name__ == "__main__":
    main()
