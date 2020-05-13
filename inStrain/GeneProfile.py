#!/usr/bin/env python

import os
import csv
import sys
import time
import glob
import psutil
import logging
import argparse
import traceback
import multiprocessing

import Bio
import numpy as np
import pandas as pd
from tqdm import tqdm
from Bio import SeqIO
import concurrent.futures
from concurrent import futures
from inStrain import SNVprofile
from collections import defaultdict

import inStrain.SNVprofile
import inStrain.controller
import inStrain.profileUtilities

class Controller():
    '''
    The command line access point to the program
    '''
    def main(self, args):
        '''
        The main method when run on the command line
        '''
        # Parse arguments
        args = self.validate_input(args)

        vargs = vars(args)
        IS = vargs.pop('IS')
        GF = vargs.pop('gene_file')

        # Read the genes file
        logging.debug('Loading genes')
        GdbP, gene2sequence = parse_genes(GF, **vargs)

        # Calculate all your parallelized gene-level stuff
        name2result = calculate_gene_metrics(IS, GdbP, gene2sequence, **vargs)

        # Store information
        IS.store('genes_fileloc', GF, 'value', 'Location of genes file that was used to call genes')
        IS.store('genes_table', GdbP, 'pandas', 'Location of genes in the associated genes_file')
        IS.store('genes_coverage', name2result['coverage'], 'pandas', 'Coverage of individual genes')
        IS.store('genes_clonality', name2result['clonality'], 'pandas', 'Clonality of individual genes')
        IS.store('genes_SNP_density', name2result['SNP_density'], 'pandas', 'SNP density of individual genes')
        IS.store('SNP_mutation_types', name2result['SNP_mutation_types'], 'pandas', 'The mutation types of SNPs')

        if vargs.get('store_everything', False):
            IS.store('gene2sequence', gene2sequence, 'pickle', 'Dicitonary of gene -> nucleotide sequence')

        # Store the output
        out_base = IS.get_location('output') + os.path.basename(IS.get('location')) + '_'
        Gdb = get_gene_info(IS)
        Gdb.to_csv(out_base + 'gene_info.tsv', index=False, sep='\t')

        try:
            name2result['SNP_mutation_types'].to_csv(out_base + 'SNP_mutation_types.tsv', index=False, sep='\t')
        except:
            pass

    def validate_input(self, args):
        '''
        Validate and mess with the arguments a bit
        '''
        # Make sure the IS object is OK and load it
        assert os.path.exists(args.IS)
        args.IS = inStrain.SNVprofile.SNVprofile(args.IS)

        # Set up the logger
        log_loc = args.IS.get_location('log') + 'log.log'
        inStrain.controller.setup_logger(log_loc)

        return args

    # def parse_arguments(self, args):
    #     '''
    #     Argument parsing
    #     '''
    #     parser = argparse.ArgumentParser(description= """
    #         A script that runs gene-based analyses on inStrain SNV profiles\n
    #
    #         Required input: a prodigal .fna gene calls file (created with prodigal -d). Going to be opinionated about this and not support alternatives.
    #         This file is easy to recreate from other / custom data - please look under examples to see how it should be formatted.
    #         """, formatter_class=argparse.RawTextHelpFormatter)
    #
    #     # Required positional arguments
    #     parser.add_argument("-i", '--IS', help="an inStrain object", required=True)
    #     parser.add_argument("-g", "--gene_file", action="store", required=True, \
    #         help='Path to prodigal .fna genes file.')
    #     parser.add_argument("-p", "--processes", action="store", default=6, type=int, \
    #                         help='Threads to use for multiprocessing')
    #
    #     # Parse
    #     if (len(args) == 0 or args[0] == '-h' or args[0] == '--help'):
    #         parser.print_help()
    #         sys.exit(0)
    #     else:
    #         return parser.parse_args(args)

def gene_profile_worker(gene_cmd_queue, gene_result_queue, single_thread=False):
    '''
    Worker to profile splits
    '''
    while True:
        # Get command
        if not single_thread:
            cmds = gene_cmd_queue.get(True)
        else:
            try:
                cmds = gene_cmd_queue.get_nowait()
            except:
                return

        # Process cmd
        GPs = profile_genes_wrapper(cmds)
        gene_result_queue.put(GPs)

def profile_genes_wrapper(cmds):
    '''
    Take a group of commands and run geneprofile
    '''
    results = []
    for cmd in cmds:
        try:
            results.append(profile_genes(cmd.scaffold, **cmd.arguments))
        except Exception as e:
            print(e)
            traceback.print_exc()
            logging.error("FAILURE GeneException {0}".format(str(cmd.scaffold)))
            results.append(None)
    return results

def calculate_gene_metrics(IS, GdbP, gene2sequenceP, **kwargs):
    '''
    Calculate the metrics of all genes on a parallelized scaffold-level basis

    IS = Initialized inStrain.SNVprofile
    GdbP = List of gene locations
    gene2sequenceP = Dicitonary of gene -> nucleotide sequence
    '''
    logging.debug("SubPoint_genes calculate_gene_metrics start RAM is {0}".format(
                psutil.virtual_memory()[1]))

    # Get key word arguments for the wrapper
    p = int(kwargs.get('processes', 6))

    # Make a list of scaffolds to profile the genes of
    scaffolds_with_genes = set(GdbP['scaffold'].unique())
    scaffolds_in_IS = set(IS._get_covt_keys())
    scaffolds_to_profile = scaffolds_with_genes.intersection(scaffolds_in_IS)
    logging.info("{0} scaffolds with genes in the input; {1} scaffolds in the IS, {2} to compare".format(
            len(scaffolds_with_genes), len(scaffolds_in_IS), len(scaffolds_to_profile)))

    # Calculate scaffold -> number of genes to profile
    s2g = GdbP['scaffold'].value_counts().to_dict()
    kwargs['s2g'] = s2g

    # Make global objects for the profiling
    logging.debug("SubPoint_genes make_globals start RAM is {0}".format(
                psutil.virtual_memory()[1]))
    global CumulativeSNVtable
    CumulativeSNVtable = IS.get('cumulative_snv_table')
    if len(CumulativeSNVtable) > 0:
        CumulativeSNVtable = CumulativeSNVtable.sort_values('mm')
    else:
        CumulativeSNVtable = pd.DataFrame(columns=['scaffold'])

    global covTs
    covTs = IS.get('covT', scaffolds=scaffolds_to_profile)

    global clonTs
    clonTs = IS.get('clonT', scaffolds=scaffolds_to_profile)

    global gene2sequence
    gene2sequence =  gene2sequenceP

    global Gdb
    Gdb = GdbP
    logging.debug("SubPoint_genes make_globals end RAM is {0}".format(
                psutil.virtual_memory()[1]))

    # Generate commands and queue them
    logging.debug('Creating commands')
    cmd_groups = [x for x in iterate_commands(scaffolds_to_profile, Gdb, kwargs)]
    logging.debug('There are {0} cmd groups'.format(len(cmd_groups)))

    logging.debug("SubPoint_genes create_queue start RAM is {0}".format(
                psutil.virtual_memory()[1]))
    gene_cmd_queue = multiprocessing.Queue()
    gene_result_queue = multiprocessing.Queue()
    GeneProfiles = []

    for cmd_group in cmd_groups:
        gene_cmd_queue.put(cmd_group)

    logging.debug("SubPoint_genes create_queue end RAM is {0}".format(
                psutil.virtual_memory()[1]))

    if p > 1:
        logging.debug('Establishing processes')
        processes = []
        for i in range(0, p):
            processes.append(multiprocessing.Process(target=gene_profile_worker, args=(gene_cmd_queue, gene_result_queue)))
        for proc in processes:
            proc.start()

        # Set up progress bar
        pbar = tqdm(desc='Profiling genes: ', total=len(cmd_groups))

        # Get the results
        recieved_profiles = 0
        while recieved_profiles < len(cmd_groups):
            GPs = gene_result_queue.get()
            recieved_profiles += 1
            pbar.update(1)
            for GP in GPs:
                if GP is not None:
                    logging.debug(GP[4])
                    GeneProfiles.append(GP)

        # Close multi-processing
        for proc in processes:
            proc.terminate()

        # Close progress bar
        pbar.close()

    else:
        gene_profile_worker(gene_cmd_queue, gene_result_queue, single_thread=True)
        logging.info("Done profiling genes")

        # Get the genes
        recieved_profiles = 0
        while recieved_profiles < len(cmd_groups):
            GPs = gene_result_queue.get()
            recieved_profiles += 1
            pbar.update(1)
            for GP in GPs:
                if GP is not None:
                    logging.debug(GP[4])
                    GeneProfiles.append(GP)

    logging.debug("SubPoint_genes return_results start RAM is {0}".format(
                psutil.virtual_memory()[1]))
    name2result = {}
    for i, name in enumerate(['coverage', 'clonality', 'SNP_density', 'SNP_mutation_types']):
        name2result[name] = pd.concat([G[i] for G in GeneProfiles])
    logging.debug("SubPoint_genes return_results end RAM is {0}".format(
                psutil.virtual_memory()[1]))

    logging.debug("SubPoint_genes calculate_gene_metrics end RAM is {0}".format(
                psutil.virtual_memory()[1]))
    return name2result

def profile_genes(scaffold, **kwargs):
    '''
    This is the money that gets multiprocessed

    Relies on having a global "Gdb", "gene2sequence", "CumulativeSNVtable", "covTs", and "clonTs"

    * Calculate the clonality, coverage, linkage, and SNV_density for each gene
    * Determine whether each SNP is synynomous or nonsynonymous
    '''
    # Log
    pid = os.getpid()
    log_message = "\nSpecialPoint_genes {0} PID {1} whole start {2}".format(scaffold, pid, time.time())

    # For testing purposes
    if ((scaffold == 'FailureScaffoldHeaderTesting')):
        assert False

    # Get the list of genes for this scaffold
    gdb = Gdb[Gdb['scaffold'] == scaffold]

    # Calculate gene-level coverage
    log_message += "\nSpecialPoint_genes {0} PID {1} coverage start {2}".format(scaffold, pid, time.time())
    if scaffold not in covTs:
        logging.info("{0} isnt in covT!".format(scaffold))
        cdb = pd.DataFrame()
    else:
        covT = covTs[scaffold]
        cdb = calc_gene_coverage(gdb, covT)
        del covT
    log_message += "\nSpecialPoint_genes {0} PID {1} coverage end {2}".format(scaffold, pid, time.time())

    # Calculate gene-level clonality
    log_message += "\nSpecialPoint_genes {0} PID {1} clonality start {2}".format(scaffold, pid, time.time())
    if scaffold not in clonTs:
        logging.info("{0} isnt in clovT!".format(scaffold))
        cldb = pd.DataFrame()
    else:
        clonT = clonTs[scaffold]
        cldb = calc_gene_clonality(gdb, clonT)
        del clonT
    log_message += "\nSpecialPoint_genes {0} PID {1} clonality end {2}".format(scaffold, pid, time.time())

    # Calculate gene-level SNP desnsity
    log_message += "\nSpecialPoint_genes {0} PID {1} SNP_density start {2}".format(scaffold, pid, time.time())
    Ldb = CumulativeSNVtable[CumulativeSNVtable['scaffold'] == scaffold]
    if len(Ldb) == 0:
        ldb = pd.DataFrame()
    else:
        ldb = calc_gene_snp_density(gdb, Ldb)
    log_message += "\nSpecialPoint_genes {0} PID {1} SNP_density end {2}".format(scaffold, pid, time.time())

    # Determine whether SNPs are synonmous or non-synonmous
    log_message += "\nSpecialPoint_genes {0} PID {1} SNP_character start {2}".format(scaffold, pid, time.time())
    if len(Ldb) == 0:
        sdb = pd.DataFrame()
    else:
        sdb = Characterize_SNPs_wrapper(Ldb, gdb)
    log_message += "\nSpecialPoint_genes {0} PID {1} SNP_character end {2}".format(scaffold, pid, time.time())

    log_message += "\nSpecialPoint_genes {0} PID {1} whole end {2}".format(scaffold, pid, time.time())

    results = (cdb, cldb, ldb, sdb, log_message)

    return results

def calc_gene_coverage(gdb, covT):
    '''
    Gene-level and mm-level coverage
    '''
    table = defaultdict(list)

    for mm, cov in iterate_covT_mms(covT):
        if len(cov) == 0:
            continue

        for i, row in gdb.iterrows():
            gcov = cov.loc[int(row['start']):int(row['end'])]
            gLen = abs(row['end'] - row['start']) + 1

            table['gene'].append(row['gene'])
            table['coverage'].append(gcov.sum() / gLen)
            table['breadth'].append(len(gcov) / gLen)
            table['mm'].append(mm)

    return pd.DataFrame(table)

def iterate_clonT_mms(clonT):
    p2c = {}
    mms = sorted([int(mm) for mm in list(clonT.keys())])
    for mm in mms:
        for pos, val in clonT[mm].items():
            p2c[pos] = val

        inds = []
        vals = []
        for ind in sorted(p2c.keys()):
            inds.append(ind)
            vals.append(p2c[ind])

        yield mm, pd.Series(data = vals, index = np.array(inds).astype('int'))

def iterate_covT_mms(clonT):
    counts = pd.Series()
    mms = sorted([int(mm) for mm in list(clonT.keys())])
    for mm in mms:
        count = clonT[mm]
        counts = counts.add(count, fill_value=0)
        yield mm, counts

def calc_gene_clonality(gdb, clonT):
    '''
    Gene-level and mm-level clonality
    '''
    table = defaultdict(list)

    for mm, cov in iterate_clonT_mms(clonT):
        if len(cov) == 0:
            continue

        for i, row in gdb.iterrows():
            gcov = cov.loc[int(row['start']):int(row['end'])]
            gLen = abs(row['end'] - row['start']) + 1

            table['gene'].append(row['gene'])

            try:
                microdiversity = 1 - gcov.mean()
            except :
                microdiversity = np.nan

            table['clonality'].append(gcov.mean())
            table['microdiversity'].append(microdiversity)
            table['masked_breadth'].append(len(gcov) / gLen)
            table['mm'].append(mm)

    return pd.DataFrame(table)

def calc_gene_snp_density(gdb, ldb):
    '''
    Gene-level and mm-level clonality
    '''
    table = defaultdict(list)

    for mm in sorted(ldb['mm'].unique()):
        db = ldb[ldb['mm'] <= mm].drop_duplicates(subset=['scaffold', 'position'], keep='last')
        cov = db.set_index('position')['refBase'].sort_index()
        if len(cov) == 0:
            continue

        for i, row in gdb.iterrows():
            gcov = cov.loc[int(row['start']):int(row['end'])]
            gLen = abs(row['end'] - row['start']) + 1

            table['gene'].append(row['gene'])
            table['SNPs_per_bp'].append(len(gcov) / gLen)
            table['mm'].append(mm)

    return pd.DataFrame(table)

def Characterize_SNPs_wrapper(Ldb, gdb):
    '''
    A wrapper for characterizing SNPs

    RELIES ON HAVING gene2sequence AS A GLOBAL (needed for multiprocessing speed)

    Arguments:
        Ldb = CumulativeSNVtable for a single scaffold
        gdb = table of genes

    Returns:
        Sdb = The Cumulative SNV table with extra information added
    '''
    # Get a non-nonredundant list of SNPs
    Sdb = Ldb.drop_duplicates(subset=['scaffold', 'position'], keep='last')\
                .sort_index().drop(columns=['mm'])
    Sdb['position'] = Sdb['position'].astype(int)

    # Filter out SNPs that shouldn't be profiled like this
    Sdb = Sdb[Sdb['cryptic'] == False]
    Sdb = Sdb.drop(columns="cryptic")

    if 'morphia' in Sdb.columns:
        col = 'morphia'
    else:
        col = 'allele_count'
    Sdb[col] = Sdb[col].astype(int)
    Sdb = Sdb[(Sdb[col] > 0) & (Sdb[col] <= 2)]

    # Make sure some SNPs to profile remain
    if len(Sdb) == 0:
        return pd.DataFrame()

    # Characterize
    sdb = characterize_SNPs(gdb, Sdb)
    assert len(Sdb) == len(sdb)
    sdb = pd.merge(Sdb, sdb, on=['position'], how='left').reset_index(drop=True)

    # Return
    return sdb

def characterize_SNPs(gdb, Sdb):
    '''
    Determine the type of SNP (synonymous, non-synynomous, or intergenic)

    RELIES ON HAVING gene2sequence AS A GLOBAL (needed for multiprocessing speed)
    '''
    table = defaultdict(list)
    for i, row in Sdb.iterrows():
        db = gdb[(gdb['start'] <= row['position']) & (gdb['end'] >= row['position'])]
        if len(db) == 0:
            table['position'].append(row['position'])
            table['mutation_type'].append('I')
            table['mutation'].append('')
            table['gene'].append('')
        elif len(db) > 1:
            table['position'].append(row['position'])
            table['mutation_type'].append('M')
            table['mutation'].append('')
            table['gene'].append(','.join(db['gene'].tolist()))
        else:
            # Get the original sequence
            original_sequence = gene2sequence[db['gene'].tolist()[0]]
            if db['direction'].tolist()[0] == '-1':
                original_sequence = original_sequence.reverse_complement()

            # Make the new sequence
            snp_start = row['position'] - db['start'].tolist()[0]
            new_sequence = original_sequence.tomutable()
            new_sequence[snp_start] = row['varBase']
            if new_sequence[snp_start] == original_sequence[snp_start]:
                new_sequence[snp_start] = row['conBase']
            new_sequence = new_sequence.toseq()

            # Translate
            if db['direction'].tolist()[0] == '-1':
                old_aa_sequence = original_sequence.reverse_complement().translate()
                new_aa_sequence = new_sequence.reverse_complement().translate()
            else:
                old_aa_sequence = original_sequence.translate()
                new_aa_sequence = new_sequence.translate()

            # old_aa_sequence = original_sequence.translate()
            # new_aa_sequence = new_sequence.translate()

            # Find mutation
            mut_type = 'S'
            mut = 'S:' + str(snp_start)
            for aa in range(0, len(old_aa_sequence)):
                if new_aa_sequence[aa] != old_aa_sequence[aa]:
                    mut_type = 'N'
                    mut = 'N:' + str(old_aa_sequence[aa]) + str(snp_start) + str(new_aa_sequence[aa])
                    break

            # Store
            table['position'].append(row['position'])
            table['mutation_type'].append(mut_type)
            table['mutation'].append(mut)
            table['gene'].append(db['gene'].tolist()[0])

    return pd.DataFrame(table)

def iterate_commands(scaffolds_to_profile, Gdb, kwargs):
    '''
    Break into individual scaffolds
    '''
    processes = kwargs.get('processes', 6)
    s2g = kwargs.get('s2g', None)
    SECONDS = min(60, sum(calc_estimated_runtime(s2g[scaffold]) for scaffold in scaffolds_to_profile)/(processes+1))

    cmds = []
    seconds = 0
    for scaffold, gdb in Gdb.groupby('scaffold'):
        if scaffold not in scaffolds_to_profile:
            continue

        # make this comammand
        cmd = Command()
        cmd.scaffold = scaffold
        cmd.arguments = kwargs

        # Add estimated seconds
        seconds += calc_estimated_runtime(s2g[scaffold])
        cmds.append(cmd)

        # See if you're done
        if seconds >= SECONDS:
            yield cmds
            seconds = 0
            cmds = []

    yield cmds

def calc_estimated_runtime(pairs):
    SLOPE_CONSTANT = 0.01
    return pairs * SLOPE_CONSTANT



class Command():
    def __init__(self):
        pass

def parse_genes(gene_file, **kwargs):
    '''
    Parse a file of genes based on the file extention.

    Currently supported extentions are:
        .fna (prodigal)
        .gb / .gbk (genbank)

    Methods return a table of genes (Gdb) and a dictionary of gene -> sequence
    '''
    if ((gene_file[-4:] == '.fna') | (gene_file[-3:] == '.fa')):
        return parse_prodigal_genes(gene_file)

    elif ((gene_file[-3:] == '.gb') | (gene_file[-4:] == '.gbk')):
        return parse_genbank_genes(gene_file)

    else:
        print("I dont know how to process {0}".format(gene_file))
        raise Exception

def parse_prodigal_genes(gene_fasta):
    '''
    Parse the prodigal .fna file

    Return a datatable with gene info and a dictionary of gene -> sequence
    '''
    table = defaultdict(list)
    gene2sequence = {}
    for record in SeqIO.parse(gene_fasta, 'fasta'):
        gene = str(record.id)

        table['gene'].append(gene)
        table['scaffold'].append("_".join(gene.split("_")[:-1]))
        table['direction'].append(record.description.split("#")[3].strip())
        table['partial'].append('partial=00' not in record.description)

        # NOTE: PRODIGAL USES A 1-BASED INDEX AND WE USE 0, SO CONVERT TO 0 HERE
        table['start'].append(int(record.description.split("#")[1].strip())-1)
        table['end'].append(int(record.description.split("#")[2].strip())-1)

        gene2sequence[gene] = record.seq

    Gdb = pd.DataFrame(table)
    logging.debug("{0:.1f}% of the input {1} genes were marked as incomplete".format((len(Gdb[Gdb['partial'] == True])/len(Gdb))*100, len(Gdb)))

    return Gdb, gene2sequence

def parse_genbank_genes(gene_file, gene_name='gene'):
    '''
    Parse a genbank file. Gets features marked as CDS
    '''
    table = defaultdict(list)
    gene2sequence = {}
    for record in SeqIO.parse(gene_file, 'gb'):
        scaffold = record.id
        for feature in record.features:
            if feature.type == 'CDS':
                gene = feature.qualifiers[gene_name][0]
                loc = feature.location
                if type(loc) is Bio.SeqFeature.CompoundLocation:
                    partial = 'compound'
                else:
                    partial = False

                table['gene'].append(gene)
                table['scaffold'].append(scaffold)
                table['direction'].append(feature.location.strand)
                table['partial'].append(partial)

                table['start'].append(loc.start)
                table['end'].append(loc.end - 1)

                gene2sequence[gene] = feature.location.extract(record).seq

    Gdb = pd.DataFrame(table)
    logging.debug("{0:.1f}% of the input {1} genes were marked as compound".format((len(Gdb[Gdb['partial'] != False])/len(Gdb))*100, len(Gdb)))

    return Gdb, gene2sequence

def get_gene_info(IS,  ANI_level=0):
    #IS = inStrain.SNVprofile.SNVprofile(IS_loc)

     # Get the mm level
    mm = _get_mm(IS, ANI_level)

    # Load all genes
    Gdb = IS.get('genes_table')

    # Load coverage, clonality, and SNPs
    for thing in ['genes_coverage', 'genes_clonality', 'genes_SNP_density']:
        db = IS.get(thing)
        if len(db) > 0:
            db = db[db['mm'] <= mm].sort_values('mm').drop_duplicates(subset=['gene'], keep='last')
            del db['mm']
            Gdb = pd.merge(Gdb, db, on='gene', how='left')
        else:
            logging.debug('Skipping {0} gene calculation; you have none'.format(thing))

    Gdb['min_ANI'] = ANI_level

    return Gdb

def _get_mm(IS, ANI):
    '''
    Get the mm corresponding to an ANI level in an IS
    '''
    if ANI > 1:
        ANI = ANI / 100

    rLen = IS.get('read_report')['mean_pair_length'].tolist()[0]
    mm = int(round((rLen - (rLen * ANI))))
    return mm
