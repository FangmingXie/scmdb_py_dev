"""Functions used to generate content. """
import datetime
import csv
import glob
import json
import math
import os
import sys
import time

from collections import OrderedDict
from itertools import groupby, chain
from random import sample

import colorlover as cl
import colorsys
from flask import current_app
from flask import Blueprint
from numpy import nan, arange, random
import pandas
import plotly
from plotly import tools
from plotly.graph_objs import Layout, Box, Scatter, Scattergl, Scatter3d, Heatmap
import sqlite3
from sqlite3 import Error

from . import cache


content = Blueprint('content', __name__) # Flask "bootstrap"


class FailToGraphException(Exception):
    """Fail to generate data or graph due to an internal error."""
    pass


@content.route('/content/metadata/<dataset>')
def get_metadata(dataset):

    dataset_path = "{}/datasets".format(current_app.config['DATA_DIR']) 
    for root, dirs, files in os.walk(dataset_path):
        for dir in dirs:
            meta_path = "{}/{}/{}/metadata.csv".format(dataset_path, dir, dataset)
            if os.path.isfile(meta_path):
                f = open(meta_path, 'r')
                reader = csv.DictReader(f)
                out = json.dumps({"data":[row for row in reader]})
                return out
        break

    return json.dumps({"data": None})


@content.route('/content/ensemble_list')
def get_ensemble_list():
 
    ensemble_list = next(os.walk(
        "{}/ensembles".format(current_app.config['DATA_DIR'])))[1]
    ensembles_json_list = []
 
    for ensemble in ensemble_list:
 
        # Assuming each ensemble to be it's own dataset
        dataset_dir = "{}/ensembles/{}/datasets/".format(current_app.config['DATA_DIR'], ensemble)
        ens_datasets = next(os.walk(dataset_dir))[1]
        ens_dict = {"ensemble": ensemble, "datasets": "\n".join(ens_datasets)}
        for i in range(len(ens_datasets)):
            ens_dict["dataset_{}".format(i+1)] = ens_datasets[i]
        ensembles_json_list.append(ens_dict)
 
    data_dict = {"data":ensembles_json_list}
    ens_json = json.dumps(data_dict)
 
    return ens_json


# Utilities
@cache.memoize(timeout=50)
def species_exists(species):
    """Check if data for a given species exists by looking for its data directory.

    Arguments:
        species (str): Name of species.

    Returns:
        bool: Whether if given species exists
    """
    return os.path.isdir(
        '{}/ensembles/{}'.format(current_app.config['DATA_DIR'], species))


@cache.memoize(timeout=50)
def gene_exists(species, methylationType, gene):
    """Check if data for a given gene of species exists by looking for its data directory.

    Arguments:
        species (str): Name of species.
        methylationType (str): Type of methylation to visualize. "mch" or "mcg"
        gene (str): Ensembl ID of gene for that species.

    Returns:
        bool: Whether if given gene exists
    """

    datasets_path = '{}/ensembles/{}/datasets'.format(
            current_app.config['DATA_DIR'], species)

    for root, dirs, files in os.walk(datasets_path):
        for dir in dirs:
            try:
                filename = \
                    glob.glob('{}/{}/{}/{}*'.format(root, dir, methylationType, gene))[0]
                if filename:
                    return True
            except IndexError:
                continue
        
        return False


def build_hover_text(labels):
    """Build HTML for Plot.ly graph labels.

        Arguments:
            labels (dict): Dictionary of attributes to be displayed.

        Returns:
            str: Generated HTML for labels.

        Example:
            >>> build_hover_text({'Test1': 'Value1', 'Example2': 'Words2'})
            'Test1: Value1<br>Example2: Words2'

    """
    text = str()
    for k, v in labels.items():
        text += '{k}: {v}<br>'.format(k=k, v=str(v))

    return text.strip('<br>')


def generate_cluster_colors(num):
    """Generate a list of colors given number needed.

    Arguments:
        num (int): Number of colors needed. n <= 35.

    Returns:
        list: strings containing RGB-style strings e.g. rgb(255,255,255).
    """

    # Selects a random colorscale (RGB) depending on number of colors needed
    if num < 12:
        c = cl.scales[str(num)]['qual']
        c = c[random.choice(list(c))]
    else:
        num_rounded = int(math.ceil(num / 10)) * 10
        c = cl.to_rgb(cl.interp(cl.scales['12']['qual']['Paired'], num_rounded))
    c = cl.to_numeric(sample(c, int(num)))

    # Converts selected colorscale to HSL, darkens the color if it is too light,
    # convert it to rgb string and return
    c_rgb = []
    for color in c:
        hls = colorsys.rgb_to_hls(color[0] / 255, color[1] / 255, color[2] / 255)
        if hls[1] < 0.6:  # Darkens the color if it is too light (HLS = [0]Hue [1]Lightness [2]Saturation)
            rgb = colorsys.hls_to_rgb(hls[0], 0.6, hls[2])
        else:
            rgb = colorsys.hls_to_rgb(hls[0], hls[1], hls[2])
        rgb_str = "rgb(" + str(rgb[0]*255) + "," + str(rgb[1]*255) + "," + str(rgb[2]*255) + ")"
        c_rgb.append(rgb_str)

    return c_rgb


def randomize_cluster_colors(num):
    """Generates random set of colors for tSNE cluster plot.

    Arguments:
        None

    Returns:
        list: dict items.
            'colors' = new set of colors for each trace in rgb.
            'num_colors' = number of colors to be used
            'cluster_color_#' = indexes of traces to be assigned the new color

    """
    cache.delete_memoized(generate_cluster_colors)
    try:
        new_colors = {'colors': generate_cluster_colors(num)}
        new_colors['num_colors'] = num
        new_colors.update(trace_colors)
        return new_colors
    except NameError:
        time.sleep(2)
        randomize_cluster_colors(num)


def set_color_by_percentile(this, start, end):
    """Set color below or above percentiles to their given values.

    Since the Plot.ly library handles coloring, we work directly with mCH values in this function. The two percentiles
    are generated by the pandas library from the plot-generating method.

    Arguments:
        this (float): mCH value to be compared.
        start (float): Lower end of percentile.
        end (float): Upper end of percentile.

    Returns:
        int: Value of `this`, if it is within percentile limits. Otherwise return one of two percentiles.
    """
    if str(this) == 'nan':
        return 'grey'
    if this < start:
        return start
    elif this > end:
        return end
    return this


def find_orthologs(mmu_gid=str(), hsa_gid=str()):
    """Find orthologs of a gene.

    Either hsa_gID or mmu_gID should be completed.

    Arguments:
        mmu_gid (str): Ensembl gene ID of mouse.
        hsa_gid (str): Ensembl gene ID of human.

    Returns:
        dict: hsa_gID and mmu_gID as strings.
    """
    if not mmu_gid and not hsa_gid:  # Should have at least one.
        return {'mmu_gID': None, 'hsa_gID': None}

    conn = sqlite3.connect(
        '{}/datasets/orthologs.sqlite3'.format(current_app.config['DATA_DIR']))

    # This ensures dictionaries are returned for fetch results.
    conn.row_factory = sqlite3.Row  

    cursor = conn.cursor()
    query_key = 'mmu_gID' if mmu_gid else 'hsa_gID'
    query_value = mmu_gid or hsa_gid

    try:
        cursor.execute(
            'SELECT * FROM orthologs WHERE {key}=?'.format(key=query_key),
            (query_value,))
    except sqlite3.Error:
        now = datetime.datetime.now()
        print("[{}] ERROR in app: Could not load top_corr_genes.sqlite3 for {}".format(str(now), species))
        sys.stdout.flush()
        return {'mmu_gID': None, 'hsa_gID': None}

    query_results = cursor.fetchone()
    if not query_results:
        return {'mmu_gID': None, 'hsa_gID': None}
    else:
        return dict(query_results)


@cache.cached(timeout=3600)
def all_gene_modules():
    """Generate list of gene modules for populating gene modules selector.
    Arguments:
        None
    Returns:
        list of gene module names. 
    """
    try:
        filename = glob.glob('{}/gene_modules.tsv'.format(current_app.config[
            'DATA_DIR']))[0]
    except IndexError:
        now = datetime.datetime.now()
        print("[{}] ERROR in app: Could not load gene_modules.tsv".format(str(now)))
        sys.stdout.flush()
        return []
    
    df = pandas.read_csv(filename, delimiter="\t", engine='python').to_dict('records')
    modules = []
    for key, value in groupby(df, key = lambda gene: gene['module']):
        modules.append({'module': key})
    
    return modules


@cache.memoize(timeout=1800)
def get_genes_of_module(species, module):
    """Generates list of genes in selected module.
    Arguments:
        module (str): Name of module to query for.
    Returns:
        Dataframe of geneName and geneID of each gene in the module for the corresponding
        species.
    """
    try:
        filename = glob.glob('{}/gene_modules.tsv'.format(current_app.config[
            'DATA_DIR']))[0]
    except IndexError:
        now = datetime.datetime.now()
        print("[{}] Error in app: Could not load gene_modules.tsv".format(str(now)))
        sys.stdout.flush()
        return []

    df = pandas.read_csv(filename, delimiter = "\t", engine='python')
    if 'mmu' in species or 'mouse' in species:
        df = df[['module', 'mmu_geneName', 'mmu_gID']]
        df.rename(columns={'mmu_geneName': 'geneName', 'mmu_gID': 'geneID'}, inplace=True)
    else:
        df = df[['module', 'hsa_geneName', 'hsa_gID']]
        df.rename(columns={'hsa_geneName': 'geneName', 'hsa_gID': 'geneID'}, inplace=True)

    return df[df['module'] == module].to_dict('records')


@cache.memoize(timeout=3600)
def median_cluster_mch(gene_info, level, cluster_type = 'cluster_name'):
    """Returns median mch level of a gene for each cluster.

        Arguments:
            gene_info (dict): mCH data for each sample. Keys are samp(cell), tsne_x, tsne_y, cluster_label, cluster_ordered, original, normalized.
            level (str): Type of mCH data. Should be "original" or "normalized".

        Returns:
            dict: Cluster_label (key) : median mCH level (value).
    """
    df = pandas.DataFrame(gene_info)
    return df.sort_values('cluster_ordered').groupby(cluster_type, sort=False)[level].median()


@cache.memoize(timeout=3600)
def get_cluster_points(species):
    """Generate points for the tSNE cluster.
    Arguments:
        species (str): Name of species.
    Returns:
        list: cluster points in dict. See tsne_points_ordered.tsv of each species for dictionary keys.
        None: if there is an error finding the file of the species.
    """
    if not species_exists(species):
        return None

    try:
        with open('{}/ensembles/{}/tsne_points_ordered.tsv'.format(
                   current_app.config['DATA_DIR'], species)) as fp:
            return list(
                csv.DictReader(fp, delimiter='\t', quoting=csv.QUOTE_NONE))
    except IOError:
        now = datetime.datetime.now()
        print("[{}] ERROR in app: Failed to load tsne data for {}".format(str(now), species))
        sys.stdout.flush()
        return None


@cache.memoize(timeout=3600)
def is_3D_data_exists(species):
    """Determines whether 3D tSNE data for a species exists.
    Arguments:
        species (str): Name of species:
    Returns:
        bool: True if 3D tSNE data exists. Returns False otherwise.
    """
    return os.path.isfile(
        '{}/ensembles/{}/tsne_points_ordered_3D.tsv'.format(current_app.config['DATA_DIR'], species))


@cache.memoize(timeout=3600)
def get_3D_cluster_points(species):
    """Generate points for the 3D tSNE cluster.
    Arguments:
        species (str): Name of species.
    Returns:
        list: cluster points in dict. See 3D_tsne_points.tsv of each species for dictionary keys.
        None: if there is an error finding the file of the species.
    """

    if not species_exists(species):
        return None
    try:
        with open('{}/ensembles/{}/tsne_points_ordered_3D.tsv'.format(
                  current_app.config['DATA_DIR'], species)) as fp:
            return list(
                csv.DictReader(fp, dialect='excel-tab'))
    except IOError:
        now = datetime.datetime.now()
        print("[{}] ERROR in app: Could not load tsne_points_ordered_3D.tsv for {}".format(str(now)), species)
        sys.stdout.flush()
        return None

@cache.memoize(timeout=3600)
def search_gene_names(species, query):
    """Match gene names of a species.

    Arguments:
        species (str): Name of species.
        query (str): Query string of gene name.

    Returns:
        list: dict of genes found. See gene_id_to_names.csv of each species for dictionary keys. Empty if error during
            searching.
    """
    if not species_exists(species):
        return []

    conn = sqlite3.connect('{}/ensembles/{}/species/gene_names.sqlite3'.format(
        current_app.config['DATA_DIR'], species))
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT * FROM gene_names WHERE geneName LIKE ?',
                       (query + '%',))
    except sqlite3.Error:
        now = datetime.datetime.now()
        print("[{}] ERROR in app: Could not open gene_names.sqlite3 for {}".format(str(now), species))
        sys.stdout.flush()
        return []

    query_results = cursor.fetchall()
    return [dict(x) for x in query_results][:50]


@cache.memoize(timeout=3600)
def gene_id_to_name(species, query):
    """Match gene ID of a species.

        Arguments:
            species (str): Name of species.
            query (str): Query string of gene ID.

        Returns:
            dict: information of gene found. See gene_id_to_names.tsv of each species for dictionary keys.
    """
    if not species_exists(species) or query == "" or query == None:
        return []

    conn = sqlite3.connect('{}/ensembles/{}/species/gene_names.sqlite3'.format(
        current_app.config['DATA_DIR'], species))

    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT * FROM gene_names WHERE geneID LIKE ?',
                        (query + '%',))
    except sqlite3.Error:
        now = datetime.datetime.now()
        print("[{}] ERROR in app: Could not load gene_names.sqlite3 for {}".format(str(now), species))
        return None

    query_results = cursor.fetchone()
    return dict(query_results)


def convert_geneID_mmu_hsa(species, geneID):
    """Converts mmu geneIDs to hsa geneIDs and vice versa if the ID's do not correspond to
    the species being viewed. Necessary due to caching of last viewed gene IDs.

    Arguments:
        species(str): Name of species.
        geneID(str): Ensembl ID of gene.of

    Returns:
        str: correct geneID that corresponds with the species.
    """
    if species == 'mouse_published' or species == 'mmu':
        if "ENSMUSG" not in geneID:   #ENSUMG is the Ensembl header for mouse genes
            mmu = find_orthologs(hsa_gid=geneID)['mmu_gID']
            return find_orthologs(hsa_gid=geneID)['mmu_gID']
        else:
            return geneID
    else:
        if "ENSMUSG" in geneID:
            gene2 = find_orthologs(mmu_gid=geneID)['hsa_gID']
            return find_orthologs(mmu_gid=geneID)['hsa_gID']
        else:
            return geneID


def get_corr_genes(species,query):
    """Get correlated genes of a certain gene of a species. 
    
        Arguments:
            species(str): Name of species.
            query(str): Query string of gene ID.
        
        Returns:
            dict: information of genes that are correlated with target gene.
    """
    db_location = '{}/ensembles/{}/top_corr_genes.sqlite3'.format(
        current_app.config['DATA_DIR'], species)
    conn = sqlite3.connect(db_location)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        cursor.execute('SELECT Gene2, Correlation FROM corr_genes WHERE Gene1 LIKE ? ORDER BY Correlation DESC LIMIT 50', (query + '%',))
    except sqlite3.Error:
        now = datetime.datetime.now()
        print("[{}] ERROR in app: Could not load top_corr_genes.sqlite3 for {}".format(str(now), species))
        sys.stdout.flush()
        return(1)

    query_results = list(cursor.fetchall())
    table_data=[]
    for rank, item in enumerate(query_results, 1):
        gene = dict(item)
        geneInfo = gene_id_to_name(species, gene['Gene2'])
        geneInfo['Rank'] = rank
        geneInfo['Corr'] = gene['Correlation']
        table_data.append(geneInfo)
    return table_data


def get_mult_gene_methylation(species, methylationType, genes):
    """Return averaged methylation data ponts for a set of genes.

    Data from ID-to-Name mapping and tSNE points are combined for plot generation.

    Arguments:
        species (str): Name of species.
        methylationType (str): Type of methylation to visualize. "mch" or "mcg"
        genes ([str]): List of gene IDs to query.
        outliers (bool): Whether if outliers should be kept.

    Returns:
        DataFrame:
    """
    if not species_exists(species):
        return []

    datasets_path = '{}/ensembles/{}/datasets'.format(
            current_app.config['DATA_DIR'], species)

    df_methyl = pandas.DataFrame()
    for gene in genes:
        if gene_exists(species, methylationType, gene):

            filename = glob.glob('{}/{}/{}/{}*'.format(
                    datasets_path,
                    species, methylationType, gene))[0]
            print(filename)

            if not filename:
                now = datetime.datetime.now()
                print("[{}] ERROR in app: Could not find data for {} for {}".format(str(now), gene, species))
                sys.stdout.flush()
            else:
                try:
                    df_methyl = df_methyl.append(
                        pandas.read_csv(
                            filename, index_col=False,
                            delimiter="\t", header=None,
                            names=['GeneID','samp','original','normalized'],
                            engine='python'))
                except OSError as e:
                    now = datetime.datetime.now()
                    print("[{}] ERROR in app: Could not load data for {} for {}".format(str(now), gene, species))
                    sys.stdout.flush()

    df_methyl = df_methyl.groupby(by='samp', as_index=False).mean()

    df_cluster = pandas.DataFrame(get_cluster_points(species))

    df_merged = pandas.merge(
        df_cluster[['samp', 'tsne_x', 'tsne_y', 'cluster_label', 'cluster_name', 'cluster_ordered', 'cluster_ortholog']],
        df_methyl[['samp', 'original', 'normalized']],
        on='samp',
        how='left')

    return df_merged.sort_values(
        by='cluster_ordered', ascending=True).to_dict('records')


@cache.memoize(timeout=3600)
def get_gene_methylation(species, methylationType, gene, outliers):
    """Return mCH data points for a given gene.

    Data from ID-to-Name mapping and tSNE points are combined for plot generation.

    Arguments:
        species (str): Name of species.
        methylationType (str): Type of methylation to visualize. "mch" or "mcg"
        gene (str): Name of gene for that species.
        outliers (bool): Whether if outliers should be kept.

    Returns:
        list: dict with mCH data for each sample. Keys are samp, tsne_x, tsne_y, cluster_label, cluster_ordered,
         original, normalized.
    """

    if not species_exists(species) or not gene_exists(species, methylationType, gene):
        return []

    cluster = pandas.DataFrame(get_cluster_points(species))

    datasets_path = '{}/ensembles/{}/datasets'.format(
            current_app.config['DATA_DIR'], species)

    dataframe_list = []
    root, dirs, files = next(os.walk(datasets_path))
    for dir in dirs:

        try:
            file_string = '{}/{}/{}/{}*'.format(root, dir, methylationType, gene)
            glob_list = glob.glob(file_string)
            filename = glob_list[0]
            
        except IndexError:
            continue

        try:
            mch = pandas.read_csv(
                filename,
                sep='\t',
                header=None,
                names=['gene', 'samp', 'original', 'normalized'],
                engine='python')
            dataframe_list.append(mch)
        except FileNotFoundError:
            continue

    mch = None
    if len(dataframe_list) == 0:
        return []
    elif len(dataframe_list) == 1:
        mch = dataframe_list[0]
    else:
        mch = dataframe_list[0].append(dataframe_list[1:], ignore_index=True)


    dataframe_merged = pandas.merge(
        cluster[['samp', 'tsne_x', 'tsne_y', 'cluster_label', 'cluster_name', 'cluster_ordered', 'cluster_ortholog']],
        mch[['samp', 'original', 'normalized']],
        on='samp',
        how='left')

    if not outliers:
        # Outliers not wanted, remove rows > 99%ile.
        three_std_dev = dataframe_merged['normalized'].quantile(0.99)
        dataframe_merged = dataframe_merged[dataframe_merged.normalized <
                three_std_dev]

    dataframe_merged['cluster_ordered'] = pandas.to_numeric(
            dataframe_merged['cluster_ordered'], errors='coerce')
    dataframe_merged['cluster_ordered'] = dataframe_merged[
            'cluster_ordered'].astype('category')
    return dataframe_merged.sort_values(
            by='cluster_ordered', ascending=True).to_dict('records')


@cache.memoize(timeout=3600)
def get_ortholog_cluster_order():
    """Order cluster mm_hs_homologous_cluster.txt.

    Arguments:
        None

    Returns:
        list: tuples of (species, cluster_number)
    """
    try:
        df = pandas.read_csv(
            '{}/mm_hs_homologous_cluster.txt'.format(
                current_app.config['DATA_DIR'],
                engine='python'),
            sep='\t')
    except IOError:
        now = datetime.datetime.now()
        print("[{}] ERROR in app: Could not load mm_hs_homologous_cluster.txt".format(str(now)))
        sys.stdout.flush()
        return []

    clusters = list()
    for _, row in df.iterrows():
        mmu_cluster = ('mmu', int(row['Mouse Cluster']))
        hsa_cluster = ('hsa', int(row['Human Cluster']))
        if mmu_cluster not in clusters:
            clusters.append(mmu_cluster)
        if hsa_cluster not in clusters:
            clusters.append(hsa_cluster)

    return clusters


# Plot generating
def get_cluster_plot(species, grouping):
    """Generate tSNE cluster plot.
    Arguments:
        species (str): Name of species.
        grouping (str): Which variable to use for grouping. cluster_ordered, biosample, layer or cluster_biosample
    Returns:
        str: HTML generated by Plot.ly.
    """

    layout2d = Layout(
        autosize=True,
        height=450,
        showlegend=True,
        margin={'l': 49,
                'r': 0,
                'b': 30,
                't': 50,
                'pad': 10
                },
        legend={
            'orientation': 'v',
            'traceorder': 'grouped',
            'tracegroupgap': 4,
            'x': 1.03,
            'font': {
                'color': 'rgba(1,2,2,1)',
                'size': 10
            },
        },
        xaxis={
            'title': 'tSNE 1',
            'titlefont': {
                'color': 'rgba(1,2,2,1)',
                'size': 14,
            },
            'type': 'linear',
            'ticks': '',
            'showticklabels': False,
            'tickwidth': 0,
            'showline': True,
            'showgrid': False,
            'zeroline': False,
            'linecolor': 'black',
            'linewidth': 0.5,
            'mirror': True
        },
        yaxis={
            'title': 'tSNE 2',
            'titlefont': {
                'color': 'rgba(1,2,2,1)',
                'size': 14,
            },
            'type': 'linear',
            'ticks': '',
            'showticklabels': False,
            'tickwidth': 0,
            'showline': True,
            'showgrid': False,
            'zeroline': False,
            'linecolor': 'black',
            'linewidth': 0.5,
            'mirror': True,
            # 'range': [-20,20]
        },
        title='Cell clusters',
        titlefont={'color': 'rgba(1,2,2,1)',
                   'size': 16},
    )
    global trace_colors
    trace_colors = dict()

    traces_2d = OrderedDict()

    if(is_3D_data_exists(species)):
        points_3d = get_3D_cluster_points(species)
        if not (grouping in points_3d[0]):
            grouping = "cluster_ordered"
            print("**** Using cluster_ordered")

        layout3d = Layout(
            autosize=True,
            height=450,
            title='3D Cell Cluster',
            titlefont={'color': 'rgba(1,2,2,1)',
                       'size': 16},
            margin={'l': 49,
                    'r': 0,
                    'b': 30,
                    't': 50,
                    'pad': 10
                    },

            scene={
                'camera':{
                    'eye': dict(x=1.2, y=1.5, z=0.7),
                    'center': dict(x=0.25, z=-0.1)
                         },
                'aspectmode':'data',
                'xaxis':{
                    'title': 'tSNE 1',
                    'titlefont': {
                        'color': 'rgba(1,2,2,1)',
                        'size': 12
                    },
                    'type': 'linear',
                    'ticks': '',
                    'showticklabels': False,
                    'tickwidth': 0,
                    'showline': True,
                    'showgrid': False,
                    'zeroline': False,
                    'linecolor': 'black',
                    'linewidth': 0.5,
                    'mirror': True
                },
                'yaxis':{
                    'title': 'tSNE 2',
                    'titlefont': {
                        'color': 'rgba(1,2,2,1)',
                        'size': 12
                    },
                    'type': 'linear',
                    'ticks': '',
                    'showticklabels': False,
                    'tickwidth': 0,
                    'showline': True,
                    'showgrid': False,
                    'zeroline': False,
                    'linecolor': 'black',
                    'linewidth': 0.5,
                    'mirror': True
                },
                'zaxis':{
                    'title': 'tSNE 3',
                    'titlefont': {
                        'color': 'rgba(1,2,2,1)',
                        'size': 12
                    },
                    'type': 'linear',
                    'ticks': '',
                    'showticklabels': False,
                    'tickwidth': 0,
                    'showline': True,
                    'showgrid': False,
                    'zeroline': False,
                    'linecolor': 'black',
                    'linewidth': 0.5,
                    'mirror': True
                }
            }
        )

        max_cluster = int(
            max(points_3d, key=lambda x: int(x['cluster_ordered']))['cluster_ordered']) + 1
        if species == 'mmu' or species == 'mouse_published':
            max_cluster = 16
        num_colors = int(
            max(points_3d, key=lambda x: int(x[grouping]))[grouping]) + 1
        colors = generate_cluster_colors(num_colors)
        symbols = ['circle', 'square', 'cross', 'triangle-up', 'triangle-down', 'octagon', 'star', 'diamond']
        traces_3d = OrderedDict()
        for point in points_3d:
            cluster_num = int(point['cluster_ordered'])
            biosample_name = ""
            if 'biosample' in point:
                biosample = int(point.get('biosample', 1)) - 1
                if 'biosample_name' in point:
                    biosample_name = str(point.get('biosample_name', 'N/A'))
                else:
                    biosample_name = 'hv' + str(biosample + 1) 
            cluster_sample_num = int(point[grouping]) + max_cluster * biosample
            color_num = int(point[grouping])-1

            if grouping == 'cluster_annotation_ordered':
                legend_group = point['cluster_annotation']
                if legend_group == '':
                    legend_group = "N/A"
                trace_name_str = legend_group + " (" + biosample_name + ")"
                cluster_sample_num = int(point[grouping]) + (max_cluster * biosample)
            elif grouping == 'cluster_ordered':
                legend_group = point[grouping]
                cluster_annotation = ''
                if 'cluster_annotation' in point:
                    annotation = point['cluster_annotation']
                    if annotation == '' or annotation == None:
                        annotation = 'N/A'
                    cluster_annotation = "(" + annotation + ")"
                trace_name_str = cluster_annotation + point['cluster_name'] + " " + biosample_name
            else: # If grouping == 'biosample'
                legend_group = point[grouping]
                trace_name_str = "Sample " + biosample_name

            trace2d = traces_2d.setdefault(cluster_sample_num,
                Scattergl(
                    x=list(),
                    y=list(),
                    text=list(),
                    mode='markers',
                    visible=True,
                    name=trace_name_str,
                    legendgroup=legend_group,
                    marker={
                        'color': colors[color_num],
                        'size': 6,
                        'opacity':0.8,
                        'symbol': symbols[biosample],  # Eran and Fangming 09/12/2017
                        'line': {'width': 0.5, 'color':'black'}
                    },
                    hoverinfo='text'))
            trace2d['x'].append(point['tsne_x'])
            trace2d['y'].append(point['tsne_y'])
            if 'cluster_annotation' in point:
                trace2d['text'].append(
                    build_hover_text({
                        'Cell': point.get('samp', 'N/A'),
                        'Layer': point.get('layer', 'N/A'),
                        'Biosample': point.get('biosample_name', 'N/A'),
                        'Cluster': point.get('cluster_name', 'N/A'),
                        'Annotation': point.get('cluster_annotation', 'N/A')
                    })
                )
            else:
                trace2d['text'].append(
                    build_hover_text({
                        'Cell': point.get('samp', 'N/A'),
                        'Layer': point.get('layer', 'N/A'),
                        'Biosample': point.get('biosample_name', 'N/A'),
                        'Cluster': point.get('cluster_name', 'N/A')
                    })
                )
            trace3d = traces_3d.setdefault(cluster_sample_num,
                Scatter3d(
                    x=list(),
                    y=list(),
                    z=list(),
                    text=list(),
                    mode='markers',
                    visible=True,
                    name=trace_name_str,
                    legendgroup=legend_group,
                    marker={
                        'size': 4,
                        'symbol': symbols[biosample],  # Eran and Fangming 09/12/2017
                        'line': {'width': 1, 'color': 'black'},
                        'color': colors[color_num],
                    },
                    hoverinfo='text'))
            trace3d['x'].append(point['tsne_1'])
            trace3d['y'].append(point['tsne_2'])
            trace3d['z'].append(point['tsne_3'])
            trace3d['text'] = trace2d['text']

        for i, (key, value) in enumerate(sorted(traces_2d.items())):
            try:
                trace_str = 'cluster_color_' + str(int(value['legendgroup']) - 1)
            except:
                trace_str = 'cluster_color_' + str(value['legendgroup'])
            trace_colors.setdefault(trace_str, []).append(i)

        if species == 'mmu' or species == 'mouse_published':
            for i in range(17, 23, 1):
                traces_2d[i]['marker']['size'] = traces_3d[i]['marker']['size'] = 15
                traces_2d[i]['marker']['symbol'] = traces_3d[i]['marker']['symbol'] = symbols[i % len(symbols)]
                traces_2d[i]['marker']['color'] = traces_3d[i]['marker']['color'] = 'black'
                traces_2d[i]['visible'] = traces_3d[i]['visible'] = "legendonly"

        return {'traces_2d': traces_2d, 'traces_3d': traces_3d, 'layout2d': layout2d, 'layout3d': layout3d}

    # 3D data does not exist, only process 2D data
    else:
        points_2d = get_cluster_points(species)

        if not points_2d:
            raise FailToGraphException
        if not (grouping in points_2d[0]):
            grouping="cluster_ordered"
            print("**** Using cluster_ordered")

        max_cluster = int(
            max(points_2d, key=lambda x: int(x['cluster_ordered']))['cluster_ordered']) + 1
        if species == 'mmu' or species == 'mouse_published':
            max_cluster = 16
        num_colors = int(
                max(points_2d, key=lambda x: int(x[grouping]))[grouping])
        colors = generate_cluster_colors(num_colors)
        symbols = ['circle', 'square', 'cross', 'triangle-up', 'triangle-down', 'octagon', 'star', 'diamond']
        for point in points_2d:
            cluster_num = int(point['cluster_ordered'])
            biosample_name = ""
            biosample = 0;
            if 'biosample' in point:
                biosample = int(point.get('biosample', 1)) - 1
                if 'biosample_name' in point:
                    biosample_name = str(point.get('biosample_name', 'N/A'))
                else:
                    biosample_name = 'hv' + str(biosample + 1) 
            cluster_sample_num = int(point['cluster_ordered']) + max_cluster * biosample
            color_num = int(point[grouping]) - 1
            if grouping == 'cluster_annotation_ordered':
                legend_group = point['cluster_annotation']
                if legend_group == '':
                    legend_group = "N/A"
                trace_name_str = legend_group + " (" + biosample_name + ")"
                cluster_sample_num = int(point[grouping]) + (max_cluster * biosample)
            elif grouping == 'cluster_ordered':
                legend_group = point[grouping]
                cluster_annotation = ''
                if 'cluster_annotation' in point:
                    annotation = point['cluster_annotation']
                    if annotation == '' or annotation == None:
                        annotation = 'N/A'
                    cluster_annotation = "(" + annotation + ")"
                trace_name_str = cluster_annotation + point['cluster_name'] + " " + biosample_name
            else: # If grouping == 'biosample'
                legend_group = point[grouping]
                trace_name_str = "Sample " + biosample_name

            trace2d = traces_2d.setdefault(cluster_sample_num,
                                          Scattergl(
                                              x=list(),
                                              y=list(),
                                              text=list(),
                                              mode='markers',
                                              visible=True,
                                              name=trace_name_str,
                                              legendgroup=legend_group,
                                              marker={
                                                  'color': colors[color_num],
                                                  'size': 4,
                                                  'opacity':0.8,
                                                  'symbol': symbols[biosample],  # Eran and Fangming 09/12/2017
                                                  'line': {'width': 0.1, 'color': 'black'}
                                              },
                                              hoverinfo='text'))
            trace2d['x'].append(point['tsne_x'])
            trace2d['y'].append(point['tsne_y'])
            if 'cluster_annotation' in point:
                trace2d['text'].append(
                    build_hover_text({
                        'Cell': point.get('samp', 'N/A'),
                        'Layer': point.get('layer', 'N/A'),
                        'Biosample': point.get('biosample_name', 'N/A'),
                        'Cluster': point.get('cluster_name', 'N/A'),
                        'Annotation': point.get('cluster_annotation', 'N/A')
                    })
                )
            else:
                trace2d['text'].append(
                    build_hover_text({
                        'Cell': point.get('samp', 'N/A'),
                        'Layer': point.get('layer', 'N/A'),
                        'Biosample': point.get('biosample_name', 'N/A'),
                        'Cluster': point.get('cluster_name', 'N/A')
                    })
                )
        for i, (key, value) in enumerate(sorted(traces_2d.items())):
            try:
                trace_str = 'cluster_color_' + str(int(value['legendgroup'])-1)
            except:
                trace_str = 'cluster_color_' + str(value['legendgroup'])
            trace_colors.setdefault(trace_str, []).append(i)

        if species == 'mmu' or species == 'mouse_published':
            for i in range(17, 23, 1):
                traces_2d[i]['marker']['size'] = 15
                traces_2d[i]['marker']['symbol'] = symbols[i % len(symbols)]
                traces_2d[i]['marker']['color'] = 'black'
                traces_2d[i]['visible'] = "legendonly"
        return {'traces_2d': traces_2d, 'layout2d': layout2d}


@cache.memoize(timeout=1800)
def get_methylation_scatter(species, methylationType, query, level, ptile_start, ptile_end):
    """Generate gene body mCH scatter plot.

    x- and y-locations are based on tSNE cluster data.

    Arguments:
        species (str): Name of species.
        methylationType (str): Type of methylation to visualize. "mch" or "mcg"
        gene (str):  Ensembl ID of gene(s) for that species.
        level (str): Type of mCH data. Should be "original" or "normalized".
        ptile_start (float): Lower end of color percentile. [0, 1].
        ptile_end (float): Upper end of color percentile. [0, 1].

    Returns:
        str: HTML generated by Plot.ly.
    """
    query = query.split()
    genes = []
    for gene in query:
        genes.append(convert_geneID_mmu_hsa(species,gene))

    if methylationType == 'mch':
        titleMType = 'mCH'
    else:
        titleMType = 'mCG'

    geneName = ''
    x, y, text, mch = list(), list(), list(), list()

    if len(genes) == 1:
        points = get_gene_methylation(species, methylationType, genes[0], True)
        geneName = gene_id_to_name(species, genes[0])['geneName']
        title = 'Gene body ' + titleMType + ': ' + geneName
    else:
        points = get_mult_gene_methylation(species, methylationType, genes)
        for gene in genes:
            geneName += gene_id_to_name(species, gene)['geneName'] + '+'
        geneName = geneName[:-1]
        title = 'Avg. Gene body ' + titleMType + ': ' + geneName

    if not points:
        raise FailToGraphException

    for point in points:
        x.append(point['tsne_x'])
        y.append(point['tsne_y'])
        if level == 'normalized':
            methylation_value = point['normalized']
        else:
            methylation_value = point['original']
        mch.append(methylation_value)
        text.append(
            build_hover_text({
                titleMType: round(methylation_value, 6),
                'Sample': point['samp'],
                'Cluster': point['cluster_name']
            }))

    mch_dataframe = pandas.DataFrame(mch)
    start = mch_dataframe.dropna().quantile(ptile_start)[0].tolist()
    end = mch_dataframe.dropna().quantile(ptile_end).values[0].tolist()
    mch_colors = [set_color_by_percentile(x, start, end) for x in mch]

    colorbar_tickval = list(arange(start, end, (end - start) / 4))
    colorbar_tickval[0] = start
    colorbar_tickval.append(end)
    colorbar_ticktext = [
        str(round(x, 3)) for x in arange(start, end, (end - start) / 4)
    ]
    colorbar_ticktext[0] = '<' + str(round(start, 3))
    colorbar_ticktext.append('>' + str(round(end, 3)))

    trace = Scatter(
        mode='markers',
        x=x,
        y=y,
        text=text,
        marker={
            'color': mch_colors,
            'colorscale': 'Viridis',
            'size': 4,
            'colorbar': {
                'x': 1.05,
                'len': 0.5,
                'thickness': 10,
                'title': level.capitalize() + ' ' + titleMType,
                'titleside': 'right',
                'tickmode': 'array',
                'tickvals': colorbar_tickval,
                'ticktext': colorbar_ticktext,
                'tickfont': {'size': 10}
            }
        },
        hoverinfo='text')
    layout = Layout(
        autosize=True,
        height=450,
        title=title,
        titlefont={'color': 'rgba(1,2,2,1)',
                   'size': 16},
        margin={'l': 49,
                'r': 0,
                'b': 30,
                't': 50,
                'pad': 0},
        xaxis={
            'title': 'tSNE 1',
            'titlefont': {
                'color': 'rgba(1,2,2,1)',
                'size': 14
            },
            'type': 'linear',
            'ticks': '',
            'tickwidth': 0,
            'showticklabels': False,
            'showline': True,
            'showgrid': False,
            'zeroline': False,
            'linecolor': 'black',
            'linewidth': 0.5,
            'mirror': True,
        },
        yaxis={
            'title': 'tSNE 2',
            'titlefont': {
                'color': 'rgba(1,2,2,1)',
                'size': 14
            },
            'type': 'linear',
            'ticks': '',
            'tickwidth': 0,
            'showticklabels': False,
            'showline': True,
            'showgrid': False,
            'zeroline': False,
            'linecolor': 'black',
            'linewidth': 0.5,
            'mirror': True,
            # 'range': [-20,20]
        },
        hovermode='closest')


    # Available colorscales:
    # https://community.plot.ly/t/what-colorscales-are-available-in-plotly-and-which-are-the-default/2079
    updatemenus = list([
        dict(
            buttons=list([
                dict(
                    args=['marker.colorscale', 'Viridis'],
                    label='Viridis',
                    method='restyle'
                ),
                dict(
                    args=['marker.colorscale', 'Bluered'],
                    label='Bluered',
                    method='restyle'
                ),
                dict(
                    args=['marker.colorscale', 'Blackbody'],
                    label='Blackbody',
                    method='restyle'
                ),
                dict(
                    args=['marker.colorscale', 'Electric'],
                    label='Electric',
                    method='restyle'
                ),
                dict(
                    args=['marker.colorscale', 'Earth'],
                    label='Earth',
                    method='restyle'
                ),
                dict(
                    args=['marker.colorscale', 'Jet'],
                    label='Jet',
                    method='restyle'
                ),
                dict(
                    args=['marker.colorscale', 'Rainbow'],
                    label='Rainbow',
                    method='restyle'
                ),
                dict(
                    args=['marker.colorscale', 'Picnic'],
                    label='Picnic',
                    method='restyle'
                ),
                dict(
                    args=['marker.colorscale', 'Portland'],
                    label='Portland',
                    method='restyle'
                ),
                dict(
                    args=['marker.colorscale', 'YlGnBu'],
                    label='YlGnBu',
                    method='restyle'
                )
            ]),
            direction='down',
            showactive=True,
            x=0,
            xanchor='left',
            y=1,
            yanchor='top'
        )
    ])
    layout['updatemenus'] = updatemenus

    return plotly.offline.plot(
        {
            'data': [trace],
            'layout': layout
        },
        output_type='div',
        show_link=True,
        include_plotlyjs=False)

@cache.memoize(timeout=3600)
def get_mch_heatmap(species, methylationType, level, ptile_start, ptile_end, normalize_row, query):
    """Generate mCH heatmap comparing multiple genes.

    Arguments:
        species (str): Name of species.
        methylationType (str): Type of methylation to visualize.        "mch" or "mcg"
        level (str): Type of mCH data. Should be "original" or "normalized".
        outliers (bool): Whether if outliers should be displayed.
        ptile_start (float): Lower end of color percentile. [0, 1].
        ptile_end (float): Upper end of color percentile. [0, 1].
        normalize_row (bool): Whether to normalize by each row (gene). 
        query ([str]): Ensembl IDs of genes to display.

    Returns:
        str: HTML generated by Plot.ly
    """

    if methylationType == 'mch':
        titleMType = 'mCH'
    else:
        titleMType = 'mCG'

    title = "Gene body " + titleMType + " by cluster: "
    genes = [convert_geneID_mmu_hsa(species,gene) for gene in query.split()]

    gene_info_df = pandas.DataFrame()
    for geneID in genes:
        geneName = gene_id_to_name(species, geneID)['geneName']
        title += geneName + "+"
        gene_info_df[geneName] = median_cluster_mch(get_gene_methylation(species, methylationType, geneID, True), level)

    if normalize_row:
        for gene in gene_info_df:
            # z-score
            # gene_info_df[gene] = (gene_info_df[gene] - gene_info_df[gene].mean()) / gene_info_df[gene].std()
            # min-max
            gene_info_df[gene] = (gene_info_df[gene] - gene_info_df[gene].min()) / (gene_info_df[gene].max() - gene_info_df[gene].min())
    gene_info_dict = gene_info_df.to_dict(into=OrderedDict)    
    title = title[:-1]

    x, y, text, hover, mch = list(), list(), list(), list(), list()
    i = 0
    for key in list(gene_info_dict.keys()):
        j = 0
        y.append(key)
        mch.append(list(gene_info_dict[key].values()))
        for cluster in list(gene_info_dict[key].keys()):
            x.append(cluster)
            text.append(build_hover_text({
                'Gene': key,
                'Cluster': x[j],
                titleMType: mch[i][j]
            }))
            j += 1
        hover.append(text)
        text = []
        i += 1

    flat_mch = list(chain.from_iterable(mch))
    mch_dataframe = pandas.DataFrame(flat_mch).dropna()
    start = mch_dataframe.quantile(0.05)[0].tolist()
    end = mch_dataframe.quantile(0.95).values[0].tolist()

    colorbar_tickval = list(arange(start, end, (end - start) / 4))
    colorbar_tickval[0] = start
    colorbar_tickval.append(end)
    colorbar_ticktext = [
        str(round(x, 3)) for x in arange(start, end, (end - start) / 4)
    ]
    if normalize_row == True:
        colorbar_ticktext[0] = str(round(start, 3))
    else:
        colorbar_ticktext[0] = '<' + str(round(start, 3))
    colorbar_ticktext.append('>' + str(round(end, 3)))

    # Due to a weird bug(?) in plotly, the number of elements in tickvals and ticktext 
    # must be greater than or equal to number of genes in query. Else, javascript throws 
    # Uncaught Typeerrors when trying to hover over genes. (Tomo 12/11/17)
    while len(colorbar_tickval) < len(genes):
        colorbar_tickval.insert(0,start)
        if normalize_row == True:
            colorbar_ticktext.insert(0, str(round(start, 3)))
        else:
            colorbar_ticktext.insert(0, '<' + str(round(start, 3)))

    trace = Heatmap(
        x=x,
        y=y,
        z=mch,
        text=hover,
        colorscale='Viridis',
        colorbar={
            'x': 1.0,
            'len': 1,
            'title': level.capitalize() + ' ' + titleMType,
            'titleside': 'right',
            'tickmode': 'array',
            'tickvals': colorbar_tickval,
            'ticktext': colorbar_ticktext,
            'thickness': 10,
            'tickfont': {'size': 10}
            },
        hoverinfo='text'
        )

    layout = Layout(
        title=title,
        height=450,
        titlefont={'color': 'rgba(1,2,2,1)',
                   'size': 16},
        autosize=True,
        xaxis={
            'side': 'bottom',
            'tickangle': -45,
            'tickfont': {'size': 12}
               },
        yaxis={
            'title': 'Genes',
            'tickangle': 15,
            'tickfont': {'size': 12}
            },
        hovermode='closest'
        )

    # Available colorscales:
    # https://community.plot.ly/t/what-colorscales-are-available-in-plotly-and-which-are-the-default/2079
    updatemenus = list([
        dict(
            buttons=list([
                dict(
                    args=['colorscale', 'Viridis'],
                    label='Viridis',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Bluered'],
                    label='Bluered',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Blackbody'],
                    label='Blackbody',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Electric'],
                    label='Electric',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Earth'],
                    label='Earth',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Jet'],
                    label='Jet',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Rainbow'],
                    label='Rainbow',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Picnic'],
                    label='Picnic',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Portland'],
                    label='Portland',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'YlGnBu'],
                    label='YlGnBu',
                    method='restyle'
                )
            ]),
            direction='down',
            showactive=True,
            x=0,
            xanchor='left',
            y=1.17,
            yanchor='top'
        )
    ])

    layout['updatemenus'] = updatemenus

    return plotly.offline.plot(
        {
            'data': [trace],
            'layout': layout
        },
        output_type='div',
        show_link=True,
        include_plotlyjs=False)


@cache.memoize(timeout=3600)
def get_mch_heatmap_two_species(species, methylationType, level, ptile_start, ptile_end, normalize_row, query):
    """Generate gene body mCH heatmap for two species.

    Traces are grouped by cluster and ordered by mm_hs_homologous_cluster.txt.

    Arguments:
        species (str): Species being viewed.
        methylationType (str): Type of methylation to visualize. "mch" or "mcg"
        level (str): Type of mCH data. Should be "original" or "normalized".
        ptile_start (float): Lower end of color percentile. [0, 1].
        ptile_end (float): Upper end of color percentile. [0, 1].
        normalize_row (bool): Whether to normalize by each row (gene). 
        query ([str]):  List of ensembl IDs of genes.

    Returns:
        str: HTML generated by Plot.ly.
    """
    
    """ NOTES
    
        Must be on same colorscale, which means original heatmap must be replotted
            Color bar should be to right of right most map. Subplots?
                https://plot.ly/python/subplots/
        Ideally two separated 
        Left/Right vs. Up/Down
        if ortholog doesn't exist, fill with NaN
        Titles?
        Y-axis?

    """

    gene_mch_hsa_df = pandas.DataFrame()
    gene_mch_mmu_df = pandas.DataFrame()
    gene_mch_combined_df = pandas.DataFrame()

    if species == 'mouse_published' or species == 'mmu':
        geneID_list_mmu = [ convert_geneID_mmu_hsa(species, geneID) for geneID in query.split() ]
        geneID_list_hsa = [ find_orthologs(mmu_gid = geneID)['hsa_gID'] for geneID in geneID_list_mmu ]
        for geneID in geneID_list_mmu:
            gene_label_mmu = gene_id_to_name('mouse_published', geneID)['geneName']
            gene_mch_mmu_df[gene_label_mmu] = median_cluster_mch(get_gene_methylation('mouse_published', methylationType, geneID, True), level, cluster_type = 'cluster_ortholog')
        index = 0
        for geneID in geneID_list_hsa:
            if geneID == None or geneID == '':
                gene_label_hsa = "*N/A " + gene_mch_mmu_df.columns.values[index].upper() # ex. Cacna2d2 (mmu) -> CACNA2D2 (hsa)
                gene_mch_hsa_df[gene_label_hsa] = nan
            else:
                gene_label_hsa = gene_id_to_name('human_hv1_published', geneID)['geneName']
                gene_mch_hsa_df[gene_label_hsa] = median_cluster_mch(get_gene_methylation('human_hv1_published', methylationType, geneID, True), level, cluster_type = 'cluster_ortholog')
            index += 1
    else:
        geneID_list_hsa = [ convert_geneID_mmu_hsa(species, geneID) for geneID in query.split() ]
        geneID_list_mmu = [ find_orthologs(hsa_gid = geneID)['mmu_gID'] for geneID in geneID_list_hsa ]
        for geneID in geneID_list_hsa:
            gene_label_hsa = gene_id_to_name('human_hv1_published', geneID)['geneName'] 
            gene_mch_hsa_df[gene_label_hsa] = median_cluster_mch(get_gene_methylation('human_hv1_published', methylationType, geneID, True), level, cluster_type = 'cluster_ortholog')
        index = 0
        for geneID in geneID_list_mmu:
            if geneID == None or geneID == '':
                gene_label_mmu = "*N/A " + gene_mch_hsa_df.columns.values[index].title() # ex. CACNA2D2 (hsa) -> Cacna2d2 (mmu)
                gene_mch_mmu_df[gene_label_mmu] = nan
            else:
                gene_label_mmu = gene_id_to_name('mouse_published', geneID)['geneName'] 
                gene_mch_mmu_df[gene_label_mmu] = median_cluster_mch(get_gene_methylation('mouse_published', methylationType, geneID, True), level, cluster_type = 'cluster_ortholog')
            index += 1

    if gene_mch_hsa_df.empty or gene_mch_mmu_df.empty:
        return FailToGraphException
    
    gene_mch_combined_df = gene_mch_hsa_df.join(gene_mch_mmu_df, how='outer')
    gene_mch_combined_df = gene_mch_combined_df[gene_mch_combined_df.index != '']

    if normalize_row:
        for gene in gene_mch_combined_df:
            gene_mch_combined_df[gene] = (gene_mch_combined_df[gene]-gene_mch_combined_df[gene].min()) / \
                    (gene_mch_combined_df[gene].max()-gene_mch_combined_df[gene].min())  

    if methylationType == 'mch':
        titleMType = 'mCH'
    else:
        titleMType = 'mCG'

    title = "Orthologous gene body " + titleMType + " by cluster"

    mch_mmu = [ gene_mch_combined_df[geneName].tolist() for geneName in gene_mch_mmu_df.columns.values ]
    mch_hsa = [ gene_mch_combined_df[geneName].tolist() for geneName in gene_mch_hsa_df.columns.values ]

    text_mmu, text_hsa, hover_mmu, hover_hsa = list(), list(), list(), list()
    for geneName in gene_mch_mmu_df.columns.values:
        for cluster in gene_mch_combined_df.index:
            text_mmu.append(build_hover_text({
                    'Gene': geneName,
                    'Cluster': cluster,
                    titleMType: gene_mch_combined_df[geneName][cluster],
                    })
                )   
        hover_mmu.append(text_mmu)
        text_mmu = []
    for geneName in gene_mch_hsa_df.columns.values:
        for cluster in gene_mch_combined_df.index:
            text_hsa.append(build_hover_text({
                    'Gene': geneName,
                    'Cluster': cluster,
                    titleMType: gene_mch_combined_df[geneName][cluster],
                    })
                )
        hover_hsa.append(text_hsa)
        text_hsa = []

    mch_combined = mch_mmu + mch_hsa
    flat_mch = list(chain.from_iterable(mch_combined))
    mch_dataframe = pandas.DataFrame(flat_mch).dropna()
    start = mch_dataframe.quantile(0.05)[0].tolist()
    end = mch_dataframe.quantile(0.95).values[0].tolist()
    colorbar_tickval = list(arange(start, end, (end - start) / 4))
    colorbar_tickval[0] = start
    colorbar_tickval.append(end)
    colorbar_ticktext = [
        str(round(x, 3)) for x in arange(start, end, (end - start) / 4)
    ]
    if normalize_row == True:
        colorbar_ticktext[0] = str(round(start, 3))
    else:
        colorbar_ticktext[0] = '<' + str(round(start, 3))
    colorbar_ticktext.append('>' + str(round(end, 3)))

    # Due to a weird bug(?) in plotly, the number of elements in tickvals and ticktext 
    # must be greater than or equal to number of genes in query. Else, javascript throws 
    # Uncaught Typeerrors when trying to hover over genes. (Tomo 12/11/17)
    while len(colorbar_tickval) < len(gene_mch_hsa_df.columns):
        colorbar_tickval.insert(0,start)
        if normalize_row == True:
            colorbar_ticktext.insert(0, str(round(start, 3)))
        else:
            colorbar_ticktext.insert(0, '<' + str(round(start, 3)))

    trace_hsa = Heatmap(
            y=list(gene_mch_hsa_df.columns.values),
            x=gene_mch_combined_df.index,
            xaxis='x_hsa',
            yaxis='y_hsa',
            z=mch_hsa,
            text=hover_hsa,
            colorscale='Viridis',
            showscale=True,
            colorbar={
                'x': 1.0,
                'len': 1,
                'title': level.capitalize() + ' ' + titleMType,
                'titleside': 'right',
                'tickmode': 'array',
                'tickvals': colorbar_tickval,
                'ticktext': colorbar_ticktext,
                'thickness': 10,
                'tickfont': {'size': 10}
                },
            hoverinfo='text'
            )
    trace_mmu = Heatmap(
            y=list(gene_mch_mmu_df.columns.values),    # Use hsa gene names to have same Y-axes for both
            x=gene_mch_combined_df.index,
            xaxis='x_mmu',
            yaxis='y_mmu',
            z=mch_mmu,
            text=hover_mmu,
            colorscale='Viridis',
            showscale=False,
            hoverinfo='text'
            )

    layout = Layout(
            title=title,
            titlefont={'color': 'rgba(1,2,2,1)',
                       'size': 16},
            autosize=True,
            height=450,

            hovermode='closest'
            )

    # Available colorscales:
    # https://community.plot.ly/t/what-colorscales-are-available-in-plotly-and-which-are-the-default/2079
    updatemenus = list([
        dict(
            buttons=list([
                dict(
                    args=['colorscale', 'Viridis'],
                    label='Viridis',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Bluered'],
                    label='Bluered',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Blackbody'],
                    label='Blackbody',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Electric'],
                    label='Electric',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Earth'],
                    label='Earth',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Jet'],
                    label='Jet',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Rainbow'],
                    label='Rainbow',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Picnic'],
                    label='Picnic',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'Portland'],
                    label='Portland',
                    method='restyle'
                ),
                dict(
                    args=['colorscale', 'YlGnBu'],
                    label='YlGnBu',
                    method='restyle'
                )
            ]),
            direction='down',
            showactive=True,
            x=0,
            xanchor='left',
            y=1.17,
            yanchor='top'
        )
    ])

    layout['updatemenus'] = updatemenus

    fig = tools.make_subplots(
            rows=1,
            cols=2,
            print_grid=False,
            shared_xaxes=True,
            shared_yaxes=False,
            subplot_titles=("Human_published", "Mouse_published"),
            )
    fig.append_trace(trace_hsa, row=1, col=1)
    fig.append_trace(trace_mmu, row=1, col=2)

    fig['layout'].update(layout)
    fig['layout']['xaxis1'].update({
        'side': 'bottom',
        'tickangle': -45,
        'tickfont': {
            'size': 12
             },
        'domain':[0, 0.45],
         })

    fig['layout']['xaxis2'].update({
        'side': 'bottom',
        'tickangle': -45,
        'tickfont': {
            'size': 12
             },
        'domain':[0.55, 1.0],
         })

    fig['layout']['yaxis1'].update({
        'visible':True,
        'tickangle': 15,
        'tickfont': {
            'size': 12
            },
        })

    fig['layout']['yaxis2'].update({
        'visible':True,
        'tickangle': 15,
        'tickfont': {
            'size': 12
            },
        })

    return plotly.offline.plot(
        figure_or_data=fig,
        output_type='div',
        show_link=True,
        include_plotlyjs=False)


@cache.memoize(timeout=3600)
def get_mch_box(species, methylationType, gene, level, outliers):
    """Generate gene body mCH box plot.

    Traces are grouped by cluster.

    Arguments:
        species (str): Name of species.
        methylationType (str): Type of methylation to visualize.        "mch" or "mcg"
        gene (str):  Ensembl ID of gene for that species.
        level (str): Type of mCH data. Should be "original" or "normalized".
        outliers (bool): Whether if outliers should be displayed.

    Returns:
        str: HTML generated by Plot.ly.
    """
    gene = convert_geneID_mmu_hsa(species, gene)
    points = get_gene_methylation(species, methylationType, gene, outliers)
    if not points:
        raise FailToGraphException

    traces = OrderedDict()
    max_cluster = int(
        max(points, key=lambda x: int(x['cluster_ordered']))['cluster_ordered']) + 1
    if species == 'mmu' or species == 'mouse_published':
        max_cluster = 16
    colors = generate_cluster_colors(max_cluster)
    for point in points:
        trace = traces.setdefault(int(point['cluster_ordered']), Box(
                y=list(),
                name=point['cluster_name'],
                marker={
                    'color': colors[(int(point['cluster_ordered']) - 1) % len(colors)],
                    'outliercolor': colors[(int(point['cluster_ordered']) - 1) % len(colors)],
                    'size': 6
                },
                boxpoints='suspectedoutliers',
                visible=True,
                showlegend=False,
                ))
        if level == 'normalized':
            trace['y'].append(point['normalized'])
        else:
            trace['y'].append(point['original'])

    if species == 'mmu' or species == 'mouse_published':
        for i in range(17, 23, 1):
            traces[i]['marker']['color'] = 'black'
            traces[i]['marker']['outliercolor'] = 'black'
            traces[i]['visible'] = "legendonly"

    geneName = gene_id_to_name(species, gene)
    geneName = geneName['geneName']

    if methylationType == 'mch':
        titleMType = 'mCH'
    else:
        titleMType = 'mCG'

    layout = Layout(
        autosize=True,
        height=450,
        title='Gene body ' + titleMType + ' in each cluster: ' + geneName,
        titlefont={'color': 'rgba(1,2,2,1)',
                   'size': 20},
#        legend={
#            'orientation': 'h',
#            'y': -0.3,
#            'traceorder': 'normal',
#        },
        xaxis={
            'title': 'Cluster',
            'titlefont': {
                'size': 17
            },
            'type': 'category',
            'anchor': 'y',
            'ticks': 'outside',
            'ticklen': 4,
            'tickangle': -45,
            'tickwidth': 0.5,
            'showticklabels': True,
            'tickfont': {
                'size': 12
            },
            'showline': True,
            'zeroline': False,
            'showgrid': True,
            'linewidth': 1,
            'mirror': True,
        },
        yaxis={
            'title': geneName + ' ' + level.capitalize() + ' mCH',
            'titlefont': {
                'size': 15
            },
            'type': 'linear',
            'anchor': 'x',
            'ticks': 'outside',
            # 'tickcolor': 'white',
            'ticklen': 4,
            'tickwidth': 0.5,
            'showticklabels': True,
            'tickfont': {
                'size': 12
            },
            'showline': True,
            'zeroline': False,
            'showgrid': True,
            'linewidth': 1,
            'mirror': True,
        },
    )

    return plotly.offline.plot(
        {
            'data': list(traces.values()),
            'layout': layout
        },
        output_type='div',
        show_link=True,
        include_plotlyjs=False)


@cache.memoize(timeout=3600)
def get_mch_box_two_species(methylationType, gene_mmu, gene_hsa, level, outliers):
    """Generate gene body mCH box plot for two species.

    Traces are grouped by cluster and ordered by mm_hs_homologous_cluster.txt.
    Mouse clusters red, human clusters black.

    Arguments:
        methylationType (str): Type of methylation to visualize.        "mch" or "mcg"
        gene_mmu (str):  Ensembl ID of gene mouse.
        gene_hsa (str):  Ensembl ID of gene human.
        level (str): Type of mCH data. Should be "original" or "normalized".
        outliers (bool): Whether if outliers should be displayed.

    Returns:
        str: HTML generated by Plot.ly.
    """
    gene_hsa = convert_geneID_mmu_hsa('human_hv1_published', gene_hsa)
    gene_mmu = convert_geneID_mmu_hsa('mouse_published', gene_mmu)
    points_mmu = get_gene_methylation('mouse_published', methylationType, gene_mmu, outliers)
    points_hsa = get_gene_methylation('human_hv1_published', methylationType, gene_hsa, outliers)
    cluster_order = get_ortholog_cluster_order()
    if not points_mmu or not points_hsa or not cluster_order:
        raise FailToGraphException

    geneName = gene_id_to_name('mouse_published', gene_mmu)['geneName']

    # EAM - This organizes the box plot into groups
    traces_mmu = Box(
        y=list(i.get(level) for i in points_mmu if i.get('cluster_ortholog')),
        x=list(i.get('cluster_ortholog') for i in points_mmu if i.get('cluster_ortholog')),
        marker={'color': 'red', 'outliercolor': 'red'},
        boxpoints='suspectedoutliers')
        
    traces_hsa = Box(
        y=list(i.get(level) for i in points_hsa if i.get('cluster_ortholog')),
        x=list(i.get('cluster_ortholog') for i in points_hsa if i.get('cluster_ortholog')),
        marker={'color': 'black', 'outliercolor': 'black'},
        boxpoints='suspectedoutliers')
    traces_combined = [traces_mmu, traces_hsa]

    if methylationType == 'mch':
        titleMType = 'mCH'
    else:
        titleMType = 'mCG'

    layout = Layout(
        boxmode='group',
        autosize=True,
        height=450,
        showlegend=False,
        title='Gene body ' + titleMType + ' in each cluster: ' + geneName,
        titlefont={'color': 'rgba(1,2,2,1)',
                   'size': 20},
        # legend={
        #     'orientation': 'h',
        #     'x': -0.1,
        #     'y': -0.6,
        #     'traceorder': 'normal',
        # },
        xaxis={
            'title': '',
            'titlefont': {
                'size': 14
            },
            'type': 'category',
            'anchor': 'y',
            'ticks': 'outside',
            'tickcolor': 'rgba(51,51,51,1)',
            'ticklen': 4,
            'tickwidth': 0.5,
            'tickangle': -35,
            'showticklabels': True,
            'tickfont': {
                'size': 12
            },
            'showline': False,
            'zeroline': False,
            'showgrid': True,
        },
        yaxis={
            'title': geneName+' '+level.capitalize() + ' mCH',
            'titlefont': {
                'size': 15
            },
            'type': 'linear',
            'anchor': 'x',
            'ticks': 'outside',
            'tickcolor': 'rgba(51,51,51,1)',
            'ticklen': 4,
            'tickwidth': 0.5,
            'showticklabels': True,
            'tickfont': {
                'size': 12
            },
            'showline': False,
            'zeroline': False,
            'showgrid': True,
        },
        shapes=[
            {
                'type': 'rect',
                'fillcolor': 'transparent',
                'line': {
                    'color': 'rgba(115, 115, 115, 1)',
                    'width': 1,
                    'dash': False
                },
                'yref': 'paper',
                'xref': 'paper',
                'x0': 0,
                'x1': 1,
                'y0': 0,
                'y1': 1
            },
        ],
        annotations=[{
            'text': '<b>■</b> Mouse',
            'x': 0.4,
            'y': 1.02,
            'ax': 0,
            'ay': 0,
            'showarrow': False,
            'font': {
                'color': 'red',
                'size': 12
            },
            'xref': 'paper',
            'yref': 'paper',
            'xanchor': 'left',
            'yanchor': 'bottom',
            'textangle': 0,
        }, {
            'text': '<b>■</b> Human',
            'x': 0.5,
            'y': 1.02,
            'ax': 0,
            'ay': 0,
            'showarrow': False,
            'font': {
                'color': 'Black',
                'size': 12
            },
            'xref': 'paper',
            'yref': 'paper',
            'xanchor': 'left',
            'yanchor': 'bottom',
            'textangle': 0,
        }])

    return plotly.offline.plot(
        {
            'data': traces_combined,
            'layout': layout
        },
        output_type='div',
        show_link=True,
        include_plotlyjs=False)

