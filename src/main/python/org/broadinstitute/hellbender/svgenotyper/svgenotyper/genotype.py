import json
import logging
import numpy as np
import os
import pyro
import torch

from .constants import SVTypes
from .model import SVGenotyperPyroModel
from . import io


def run(args: dict, default_dtype: torch.dtype = torch.float32):
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)-8s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S')
    pyro.enable_validation(True)
    pyro.distributions.enable_validation(True)
    pyro.clear_param_store()

    np.random.seed(args['random_seed'])
    torch.random.manual_seed(args['random_seed'])
    pyro.set_rng_seed(args['random_seed'])
    svtype = SVTypes[args['svtype']]

    base_path = os.path.join(args['model_dir'], args['model_name'])
    params = load_model(base_path)
    if params is None:
        raise RuntimeError("Model at not found: {:s}".format(base_path))

    model = SVGenotyperPyroModel(svtype=svtype, k=params['k'], tensor_dtype=default_dtype, mu_eps_pe=params['mu_eps_pe'], mu_eps_sr1=params['mu_eps_sr1'],
                                 mu_eps_sr2=params['mu_eps_sr2'], var_phi_pe=params['var_phi_pe'], var_phi_sr1=params['var_phi_sr1'],
                                 var_phi_sr2=params['var_phi_sr2'], read_length=params['read_length'],
                                 device=args['device'], loss=params['loss'])
    load_param_store(base_path, device=args['device'])
    vids_list = io.load_list(base_path + ".vids.list")
    data = io.load_tensors(base_path=base_path, svtype=svtype, tensor_dtype=default_dtype, device=args['device'])

    posterior = model.run_predictive(data=data, n_samples=args['genotype_predictive_samples'],
                                     n_iter=args['genotype_predictive_iter'])
    #stats = get_predictive_stats(samples=predictive_samples)

    discrete_posterior = model.run_discrete(data=data, svtype=svtype, n_states=model.k,
                                            log_freq=args['genotype_discrete_log_freq'],
                                            n_samples=args['genotype_discrete_samples'])
    #freq = calculate_state_frequencies(model=model, discrete_samples=discrete_samples)
    #posterior.update(get_discrete_stats(samples=discrete_samples))
    posterior.update(discrete_posterior)

    output = get_output(vids_list=vids_list, stats=posterior, params=params)
    #global_stats = get_global_stats(stats=stats, model=model)

    io.write_variant_output(output_path=args['output'], output_data=output)
    return output


def convert_type(dat):
    if isinstance(dat, np.ndarray):
        return dat.tolist()
    return dat


def get_output(vids_list: list, stats: dict, params: dict):
    n_variants = len(vids_list)
    output_list = []
    for i in range(n_variants):
        vid = vids_list[i]
        if 'eps_pe' in stats:
            eps_pe = stats['eps_pe']['mean'][i] * params['mu_eps_pe']
        else:
            eps_pe = 0
        if 'phi_pe' in stats:
            phi_pe = stats['phi_pe']['mean'][i]
        else:
            phi_pe = 0
        if 'm_pe' in stats:
            p_m_pe = stats['m_pe']['mean'][i]
        else:
            p_m_pe = 0
        #if 'eta_r' in stats:
        #    eta_r = stats['eta_r']['mean'][i] * params['mu_eta_r']
        #else:
        #    eta_r = 0
        output_list.append({
            'vid': vids_list[i],
            'freq_z': stats['z']['mean'][i, :],
            'p_m_pe': p_m_pe,
            'p_m_sr1': stats['m_sr1']['mean'][i],
            'p_m_sr2': stats['m_sr2']['mean'][i],
            #'p_m_rd': stats['m_rd']['mean'][i],
            'eps_pe': eps_pe,
            'eps_sr1': stats['eps_sr1']['mean'][i] * params['mu_eps_sr1'],
            'eps_sr2': stats['eps_sr2']['mean'][i] * params['mu_eps_sr2'],
            'phi_pe': phi_pe,
            'phi_sr1': stats['phi_sr1']['mean'][i],
            'phi_sr2': stats['phi_sr2']['mean'][i],
            #'eta_q': stats['eta_q']['mean'][i] * params['mu_eta_q'],
            #'eta_r': eta_r
        })
    return output_list


def get_global_stats(stats: dict, model: SVGenotyperPyroModel):
    global_sites = ['pi_sr1', 'pi_sr2', 'pi_pe', 'pi_rd'] #, 'lambda_pe', 'lambda_sr1', 'lambda_sr2']
    global_site_scales = {
        'pi_sr1': 1.,
        'pi_sr2': 1.,
        'pi_pe': 1.,
        #'pi_rd': 1.,
        #'lambda_pe': model.mu_lambda_pe,
        #'lambda_sr1': model.mu_lambda_sr1,
        #'lambda_sr2': model.mu_lambda_sr2
    }
    global_stats = {}
    for site in global_sites:
        if site in stats:
            global_stats[site + '_mean'] = stats[site]['mean'] * global_site_scales[site]
            global_stats[site + '_std'] = stats[site]['std'] * global_site_scales[site]
    return global_stats


def get_predictive_stats(samples: dict):
    return {key: {'mean': samples[key].astype(dtype='float').mean(axis=0).squeeze(),
                  'std': samples[key].astype(dtype='float').std(axis=0).squeeze()} for key in samples}


def get_discrete_stats(samples: dict):
    discrete_sites = ['m_pe', 'm_sr1', 'm_sr2'] #, 'm_rd']
    return {key: {'mean': samples[key].astype(dtype='float').mean(axis=0).squeeze()} for key in discrete_sites}


def calculate_state_frequencies(model: SVGenotyperPyroModel, discrete_samples: dict):
    z = discrete_samples['z']
    z_freq = np.zeros((z.shape[1], z.shape[2], model.k))
    for i in range(model.k):
        locs = z == i
        z_copy = z.copy()
        z_copy[locs] = 1
        z_copy[~locs] = 0
        z_freq[..., i] = z_copy.sum(axis=0)
    z_freq = z_freq / z_freq.sum(axis=2, dtype='float32', keepdims=True)
    m_pe_freq = discrete_samples['m_pe'].mean(axis=0)
    m_sr1_freq = discrete_samples['m_sr1'].mean(axis=0)
    m_sr2_freq = discrete_samples['m_sr2'].mean(axis=0)
    return {
        'z': z_freq,
        'm_pe': m_pe_freq,
        'm_sr1': m_sr1_freq,
        'm_sr2': m_sr2_freq
    }


def load_model(base_path: str):
    model_path = base_path + '.vars.json'
    if os.path.exists(model_path):
        with open(model_path, 'r') as f:
            return json.load(f)
    else:
        logging.warning("Could not locate model file {:s}".format(model_path))


def load_param_store(base_path: str, device: str = 'cpu'):
    pyro.clear_param_store()
    pyro.get_param_store().load(base_path + '.param_store.pyro', map_location=torch.device(device))
