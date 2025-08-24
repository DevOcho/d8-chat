#!/usr/bin/env python3

import os
import glob


def print_files_with_line_numbers(directory="."):
    # Get all .py files in the specified directory
    py_files = glob.glob(os.path.join(directory, "app/*.py"))

    for file_path in sorted(py_files):
        file_name = os.path.basename(file_path)
        print(f"\n=== {file_name} ===")

        with open(file_path, "r") as f:
            lines = f.readlines()

        for i, line in enumerate(lines, start=1):
            # Print line number and the line content, stripping trailing newline for clean output
            print(f"{i}: {line.rstrip()}")


# Example usage: print files in the current directory
print_files_with_line_numbers()
