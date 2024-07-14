#!/bin/bash
yapf -i -r --style .style.yapf --exclude '**/third_party' predicators
yapf -i -r --style .style.yapf scripts
yapf -i -r --style .style.yapf tests
yapf -i -r --style .style.yapf setup.py
docformatter -i -r . --exclude venv predicators/third_party
isort .
# python3 predicators/main.py --env satellites-markov --approach gnn_option_policy 