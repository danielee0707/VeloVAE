# VeloVAE - Variational Mixtures of ODEs for Inferring Cellular Gene Expression Dynamics
## Introduction

The rapid growth of scRNA-seq data have spurred many researches in single-cell analysis. A key problem is to understand how gene expression changes during cell development. Recently, RNA velocity provided a new way to study gene expression from a dynamical system point of view. Because cells are destroyed during measurement, any scRNA-seq data are just one or very few static snapshots of mRNA count matrices. This makes it hard to learn the gene expression kinetics, as the time information is lost.

VeloVAE is a deep generative model for learning gene expression dynamics from scRNA-seq data. The purpose of our method is to infer latent cell time and velocity simultaneously. This is achieved by using a VAE. Different from many VAE models, VeloVAE is based on the biochemical process of converting nascent mRNA molecules to mature ones via splicing. The decoder is based on a set of ordinary differential equations. VeloVAE is capable of handling more complex gene expression kinetics compared with current methods.

## Package Usage
The package depends on several main-stream packages in computational biology and machine learning, including [scanpy](https://scanpy.readthedocs.io/en/stable/), [PyTorch](https://pytorch.org/), [scikit-learn](https://scikit-learn.org/stable/). We suggest using a GPU to accelerate the training process.

A sample jupyter notebook is available [here](notebooks/velovae_example.ipynb). Notice that the dataset in the example is from [scVelo](https://scvelo.readthedocs.io/), so you would need to install scVelo.

The package has not been submitted to PyPI yet, but you can download it and import the package locally by adding the path of the package to the system path:
```python
import sys
sys.path.append(< path to the package >)
import velovae
```
Sorry for the inconvenience and we will make it available on PyPI soon!
