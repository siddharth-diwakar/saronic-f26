FROM nvcr.io/nvidia/isaac-sim:6.1.0

WORKDIR /workspace

# NVIDIA's python.sh configures the Isaac Sim Python and Kit environment.
ENTRYPOINT ["/isaac-sim/python.sh", "/workspace/src/saronic_sim/cli.py"]
