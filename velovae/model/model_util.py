from copy import deepcopy
import numpy as np
import os
from scipy.special import softmax
import torch
import torch.nn as nn
import torch.nn.functional as F
from .scvelo_util import mRNA, vectorize, tau_inv, R_squared, test_bimodality, leastsq_NxN
from sklearn.neighbors import NearestNeighbors

"""
Dynamical Model

Reference: 
Bergen, V., Lange, M., Peidli, S., Wolf, F. A., & Theis, F. J. (2020). 
Generalizing RNA velocity to transient cell states through dynamical modeling. 
Nature biotechnology, 38(12), 1408-1414.
"""
def scvPredSingle(t,alpha,beta,gamma,ts,scaling=1.0, uinit=0, sinit=0):
    beta = beta*scaling
    tau, alpha, u0, s0 = vectorize(t, ts, alpha, beta, gamma, u0=uinit, s0=sinit)
    tau = np.clip(tau,a_min=0,a_max=None)
    ut, st = mRNA(tau, u0, s0, alpha, beta, gamma)
    ut = ut*scaling
    return ut.squeeze(), st.squeeze()

def scvPred(adata, key, glist=None):
	"""
	Reproduce the full prediction of scvelo dynamical model
	"""
	ngene = len(glist) if glist is not None else adata.n_vars
	ncell = adata.n_obs
	ut, st = np.ones((adata.n_obs,ngene))*np.nan, np.ones((adata.n_obs,ngene))*np.nan
	if(glist is None):
		glist = adata.var_names.to_numpy()
	
	for i in range(ngene):
		idx = np.where(adata.var_names==glist[i])[0][0]
		item = adata.var.loc[glist[i]]
		if(len(item)==0):
			print('Gene '+glist[i]+' not found!')
			continue
			
		alpha, beta, gamma, scaling = item[f'{key}_alpha'], item['{key}_beta'], item['{key}_gamma'], item['{key}_scaling']
		ts = item[f'{key}_t_']
		scaling = item[f'{key}_scaling']
		t = adata.layers[f'{key}_t'][:,idx]
		if(np.isnan(alpha)):
			continue
		u_g, s_g = scvPredSingle(t,alpha,beta,gamma,ts,scaling)
		
		ut[:,i] = u_g
		st[:,i] = s_g
	#assert not np.any(np.isnan(ut))
	#assert not np.any(np.isnan(st))
	return ut, st

############################################################
#Shared among all VAEs
############################################################
def histEqual(t, Tmax, perc=0.95, Nbin=101):
    t_ub = np.quantile(t, perc)
    t_lb = t.min()
    delta_t = (t_ub - t_lb)/(Nbin-1)
    bins = [t_lb+i*delta_t for i in range(Nbin)]+[t.max()] 
    pdf_t, edges = np.histogram(t, bins, density=True)
    pt, edges = np.histogram(t, bins, density=False)
    
    #Perform histogram equalization
    cdf_t = np.concatenate(([0], np.cumsum(pt)))
    cdf_t = cdf_t/cdf_t[-1]
    t_out = np.zeros((len(t)))
    for i in range(Nbin):
        mask = (t>=bins[i]) & (t<bins[i+1])
        t_out[mask] = (cdf_t[i] + (t[mask]-bins[i])*pdf_t[i])*Tmax
    return t_out

############################################################
#Basic utility function to compute ODE solutions for all models
############################################################
def predSUNumpy(tau, u0, s0, alpha, beta, gamma):
    """
    (Numpy Version)
    Analytical solution of the ODE
    
    tau: [B x 1] or [B x 1 x 1] time duration starting from the switch-on time of each gene.
    u0, s0: [G] or [N type x G] initial conditions
    alpha, beta, gamma: [G] or [N type x G] generation, splicing and degradation rates
    """
    unstability = (np.abs(beta-gamma) < 1e-3)
    expb, expg = np.exp(-beta*tau), np.exp(-gamma*tau)
    
    upred = u0*expb+alpha/beta*(1-expb)
    spred = s0*expg+alpha/gamma*(1-expg)+(alpha-beta*u0)/(gamma-beta+1e-6)*(expg-expb)*(1-unstability)+(alpha-beta*u0)*tau*expg*unstability
    return np.clip(upred, a_min=0, a_max=None), np.clip(spred, a_min=0, a_max=None)


def predSU(tau, u0, s0, alpha, beta, gamma):
    """
    (PyTorch Version)
    Analytical solution of the ODE
    
    tau: [B x 1] or [B x 1 x 1] time duration starting from the switch-on time of each gene.
    u0, s0: [G] or [N type x G] initial conditions
    alpha, beta, gamma: [G] or [N type x G] generation, splicing and degradation rates
    """
    expb, expg = torch.exp(-beta*tau), torch.exp(-gamma*tau)
    unstability = (torch.abs(beta-gamma) < 1e-3).long()
    eps = 1e-6
    
    upred = u0*expb+alpha/beta*(1-expb)
    spred = s0*expg+alpha/gamma*(1-expg)+(alpha-beta*u0)/(gamma-beta+eps)*(expg-expb)*(1-unstability)+(alpha-beta*u0)*tau*expg*unstability
    return nn.functional.relu(upred), nn.functional.relu(spred)

############################################################
#Vanilla VAE
############################################################
"""
Initialization Methods

Reference: 
Bergen, V., Lange, M., Peidli, S., Wolf, F. A., & Theis, F. J. (2020). 
Generalizing RNA velocity to transient cell states through dynamical modeling. 
Nature biotechnology, 38(12), 1408-1414.
"""
def linreg(u, s):
    q = np.sum(s*s)
    r = np.sum(u*s)
    k = r/q
    if np.isinf(k) or np.isnan(k):
        k = 1.0+np.random.rand()
    return k
    
def initGene(s,u,percent,fit_scaling=False,Ntype=None):
    """
    Adopted from scvelo

    Helper Function
    Estimate alpha, beta, gamma and the latent time of a
    single gene
    s: 1D array of spliced count
    u: 1D array of unspliced count
    """
    std_u, std_s = np.std(u), np.std(s)
    scaling = std_u / std_s if fit_scaling else 1.0
    u = u/scaling
    
    #Pick Quantiles
    # initialize beta and gamma from extreme quantiles of s
    mask_s = s >= np.percentile(s, percent, axis=0)
    mask_u = u >= np.percentile(u, percent, axis=0)
    mask = mask_s & mask_u
    if(not np.any(mask)):
        mask = mask_s
    
    
    #Initialize alpha, beta and gamma
    beta = 1
    gamma = linreg(u[mask], s[mask]) + 1e-6
    if gamma < 0.05 / scaling:
        gamma *= 1.2
    elif gamma > 1.5 / scaling:
        gamma /= 1.2
    u_inf, s_inf = u[mask].mean(), s[mask].mean()
    u0_, s0_ = u_inf, s_inf
    alpha = u_inf*beta
    # initialize switching from u quantiles and alpha from s quantiles
    tstat_u, pval_u, means_u = test_bimodality(u, kde=True)
    tstat_s, pval_s, means_s = test_bimodality(s, kde=True)
    pval_steady = max(pval_u, pval_s)
    steady_u = means_u[1]
    steady_s = means_s[1]
    if pval_steady < 1e-3:
        u_inf = np.mean([u_inf, steady_u])
        alpha = gamma * s_inf
        beta = alpha / u_inf
        u0_, s0_ = u_inf, s_inf
    t_ = tau_inv(u0_, s0_, 0, 0, alpha, beta, gamma) #time to reach steady state
    tau = tau_inv(u, s, 0, 0, alpha, beta, gamma) #induction
    tau = np.clip(tau, 0, t_)
    tau_ = tau_inv(u, s, u0_, s0_, 0, beta, gamma) #repression
    tau_ = np.clip(tau_, 0, np.max(tau_[s > 0]))
    ut, st = mRNA(tau, 0, 0, alpha, beta, gamma)
    ut_, st_ = mRNA(tau_, u0_, s0_, 0, beta, gamma)
    distu, distu_ = (u - ut) / std_u, (u - ut_) / std_u
    dists, dists_ = (s - st) / std_s, (s - st_) / std_s
    res = np.array([distu ** 2 + dists ** 2, distu_ ** 2 + dists_ ** 2])
    t = np.array([tau,tau_+np.ones((len(tau_)))*t_])
    o = np.argmin(res, axis=0)
    t_latent = np.array([t[o[i],i] for i in range(len(tau))])
    
    
    return alpha, beta, gamma, t_latent, u0_, s0_, t_, scaling
    
def initParams(data, percent,fit_offset=False,fit_scaling=True):
    """
    Adopted from SCVELO

    Use the steady-state model to estimate alpha, beta,
    gamma and the latent time
    data: ncell x (2*ngene) tensor
    percent: percentage limit to pick the data
    Output: a ncellx4 2D array of parameters
    """
    ngene = data.shape[1]//2
    u = data[:,:ngene]
    s = data[:,ngene:]
    
    
    params = np.ones((ngene,4)) #four parameters: alpha, beta, gamma, scaling
    params[:,0] = np.random.rand((ngene))*np.max(u,0)
    params[:,2] = np.random.rand((ngene))*np.max(u,0)/(np.max(s,0)+1e-10)
    T = np.zeros((ngene, len(s)))
    Ts = np.zeros((ngene))
    U0, S0 = np.zeros((ngene)), np.zeros((ngene)) #Steady-1 State
    
    for i in range(ngene):
        si, ui = s[:,i], u[:,i]
        sfilt, ufilt = si[(si>0) & (ui>0)], ui[(si>0) & (ui>0)] #Use only nonzero data points
        if(len(sfilt)>3 and len(ufilt)>3):
            alpha, beta, gamma, t, u0_, s0_, ts, scaling = initGene(sfilt,ufilt,percent,fit_scaling)
            params[i,:] = np.array([alpha,beta,gamma,scaling])
            T[i, (si>0) & (ui>0)] = t
            U0[i] = u0_
            S0[i] = s0_
            Ts[i] = ts
        else:
            U0[i] = np.max(u)
            S0[i] = np.max(s)
    
    #Filter out genes
    min_r2 = 0.01
    offset, gamma = leastsq_NxN(s,u,fit_offset,perc=[100-percent,percent])
    residual = u-gamma*s
    if(fit_offset):
        residual -= offset
    r2 = R_squared(residual, total=u-u.mean(0))
    velocity_genes = (r2>min_r2) & (gamma>0.01) & (np.max(s > 0, 0) > 0) & (np.max(u > 0, 0) > 0)
    
    dist_u, dist_s = np.zeros(u.shape),np.zeros(s.shape)
    for i in range(ngene):
        upred, spred = scvPredSingle(T[i],params[i,0],params[i,1],params[i,2],Ts[i],params[i,3]) #upred has the original scale
        dist_u[:,i] = u[:,i] - upred
        dist_s[:,i] = s[:,i] - spred
    
    sigma_u = np.clip( np.std(dist_u, 0), 0.1, None)
    sigma_s = np.clip( np.std(dist_s, 0), 0.1, None)
    
    #Make sure all genes get the same total relevance score
    Rscore = ((u>0) & (s>0))*np.ones(u.shape) + ((u==0) & (s==0))*np.ones(u.shape)*0.5 + ((u==0) & (s>0))*np.ones(u.shape)*0.02 + ((u>0) & (s>0))*np.ones(u.shape)*0.1
     
    return params[:,0], params[:,1], params[:,2], params[:,3], Ts, U0, S0, sigma_u, sigma_s, T.T, Rscore


    
"""
Reinitialization based on the global time
"""
def getTsGlobal(tgl, U, S, perc):
    """
    Initialize the transition time in the original ODE model.
    """
    tsgl = np.zeros((U.shape[1]))
    for i in range(U.shape[1]):
        u,s = U[:,i],S[:,i]
        zero_mask = (u>0) & (s>0)
        mask_u, mask_s = u>=np.percentile(u,perc),s>=np.percentile(s,perc)
        tsgl[i] = np.median(tgl[mask_u & mask_s & zero_mask])
        if(np.isnan(tsgl[i])):
            tsgl[i] = np.median(tgl[(mask_u | mask_s) & zero_mask])
        if(np.isnan(tsgl[i])):
            tsgl[i] = np.median(tgl)
    assert not np.any(np.isnan(tsgl))
    return tsgl



def reinitGene(u,s,t,ts):
    """
    Applied to the regular ODE 
    Initialize the ODE parameters (alpha,beta,gamma,t_on) from
    input data and estimated global cell time.
    """
    #u1, u2: picked from induction
    mask1_u = u>np.quantile(u,0.95)
    mask1_s = s>np.quantile(s,0.95)
    u1, s1 = np.median(u[mask1_u | mask1_s]), np.median(s[mask1_s | mask1_u])
    
    if(u1 == 0 or np.isnan(u1)):
        u1 = np.max(u)
    if(s1 == 0 or np.isnan(s1)):
        s1 = np.max(s)
    
    
    t1 = np.median(t[mask1_u | mask1_s])
    if(t1 <= 0):
        tm = np.max(t[mask1_u | mask1_s])
        t1 = tm if tm>0 else 1.0
    
    mask2_u = (u>=u1*0.49)&(u<=u1*0.51)&(t<=ts) 
    mask2_s = (s>=s1*0.49)&(s<=s1*0.51)&(t<=ts) 
    if(np.any(mask2_u) or np.any(mask2_s)):
        t2 = np.median(t[mask2_u | mask2_s])
        u2, s2 = np.median(u[mask2_u]), np.median(s[mask2_s])
        t0 = max(0,np.log((u1-u2)/(u1*np.exp(-t2)-u2*np.exp(-t1))))
    else:
        t0 = 0
    beta = 1
    alpha = u1/(1-np.exp(t0-t1)) if u1>0 else 0.1*np.random.rand()
    if(alpha <= 0 or np.isnan(alpha) or np.isinf(alpha)):
        alpha = u1
    gamma = alpha/np.quantile(s,0.95)
    if(gamma <= 0 or np.isnan(gamma) or np.isinf(gamma)):
        gamma = 2.0
    return alpha,beta,gamma,t0
    
def reinitParams(U, S, t, ts):
    """
    Reinitialize the regular ODE parameters based on estimated global latent time.
    """
    G = U.shape[1]
    alpha, beta, gamma, ton = np.zeros((G)), np.zeros((G)), np.zeros((G)), np.zeros((G))
    for i in range(G):
        alpha_g, beta_g, gamma_g, ton_g = reinitGene(U[:,i], S[:,i], t, ts[i])
        alpha[i] = alpha_g
        beta[i] = beta_g
        gamma[i] = gamma_g
        ton[i] = ton_g
    return alpha, beta, gamma, ton
    


"""
ODE Solution, with both numpy (for post-training analysis or plotting) and pytorch versions (for training)
"""
def predSteadyNumpy(ts,alpha,beta,gamma):
    """
    (Numpy Version)
    Predict the steady states.
    ts: [G] switching time, when the kinetics enters the repression phase
    alpha, beta, gamma: [G] generation, splicing and degradation rates
    """
    alpha_, beta_, gamma_ = np.clip(alpha,a_min=0,a_max=None), np.clip(beta,a_min=0,a_max=None), np.clip(gamma,a_min=0,a_max=None)
    eps = 1e-6
    unstability = np.abs(beta-gamma) < 1e-3
    
    ts_ = ts.squeeze()
    expb, expg = np.exp(-beta*ts_), np.exp(-gamma*ts_)
    u0 = alpha/(beta+eps)*(1.0-expb)
    s0 = alpha/(gamma+eps)*(1.0-expg)+alpha/(gamma-beta+eps)*(expg-expb)*(1-unstability)+alpha*ts_*expg*unstability
    return u0,s0
    
def predSteady(tau_s, alpha, beta, gamma):
    """
    (PyTorch Version)
    Predict the steady states.
    tau_s: [G] time duration from ton to toff
    alpha, beta, gamma: [G] generation, splicing and degradation rates
    """
    unstability = (torch.abs(beta - gamma) < 1e-3).long()
    eps = 1e-6
    
    expb, expg = torch.exp(-beta*tau_s), torch.exp(-gamma*tau_s)
    u0 = alpha/(beta+eps)*(torch.tensor([1.0]).to(alpha.device)-expb)
    s0 = alpha/(gamma+eps)*(torch.tensor([1.0]).to(alpha.device)-expg)+alpha/(gamma-beta+eps)*(expg-expb)*(1-unstability)+alpha*tau_s*expg*unstability
    
    return u0,s0

def odeNumpy(t,alpha,beta,gamma,to,ts,scaling=None):
    """
    (Numpy Version)
    ODE Solution
    
    t: [B x 1] cell time
    alpha, beta, gamma: [G] generation, splicing and degradation rates
    to, ts: [G] switch-on and -off time
    """
    unstability = (np.abs(beta - gamma) < 1e-3)
    eps = 1e-6
    
    o = (t<=ts).astype(int)
    #Induction
    tau_on = np.clip(t-to,a_min=0,a_max=None)
    expb, expg = np.exp(-beta*tau_on), np.exp(-gamma*tau_on)
    uhat_on = alpha/(beta+eps)*(1.0-expb)
    shat_on = alpha/(gamma+eps)*(1.0-expg)+alpha/(gamma-beta+eps)*(expg-expb)*(1-unstability)+alpha*tau_on*unstability
    
    #Repression
    u0_,s0_ = predSteadyNumpy(ts-to,alpha,beta,gamma) #[G]
    if(ts.ndim==2 and to.ndim==2):
        u0_ = u0_.reshape(-1,1)
        s0_ = s0_.reshape(-1,1)
    tau_off = np.clip(t-ts,a_min=0,a_max=None)
    expb, expg = np.exp(-beta*tau_off), np.exp(-gamma*tau_off)
    uhat_off = u0_*expb
    shat_off = s0_*expg+(-beta*u0_)/(gamma-beta+eps)*(expg-expb)*(1-unstability)
    
    uhat, shat = (uhat_on*o + uhat_off*(1-o)),(shat_on*o + shat_off*(1-o))
    if(scaling is not None):
        uhat *= scaling
    return uhat, shat

def ode(t,alpha,beta,gamma,to,ts,train_mode=False,neg_slope=0):
    """
    (PyTorch Version)
    ODE Solution
    
    t: [B x 1] cell time
    alpha, beta, gamma: [G] generation, splicing and degradation rates
    to, ts: [G] switch-on and -off time
    """
    unstability = (torch.abs(beta - gamma) < 1e-3).long()
    eps = 1e-6
    o = (t<=ts).int()
    
    #Induction
    tau_on = nn.functional.leaky_relu(t-to, negative_slope=neg_slope) if train_mode else nn.functional.relu(t-to) 
    expb, expg = torch.exp(-beta*tau_on), torch.exp(-gamma*tau_on)
    uhat_on = alpha/(beta+eps)*(torch.tensor([1.0]).to(alpha.device)-expb)
    shat_on = alpha/(gamma+eps)*(torch.tensor([1.0]).to(alpha.device)-expg)+ (alpha/(gamma-beta+eps)*(expg-expb)*(1-unstability) + alpha*tau_on*expg * unstability)
    
    assert not torch.any(torch.isnan(uhat_on))
    assert not torch.any(torch.isnan(shat_on))
    
    #Repression
    u0_,s0_ = predSteady(nn.functional.leaky_relu(ts-to, neg_slope),alpha,beta,gamma) if train_mode else predSteady(nn.functional.relu(ts-to),alpha,beta,gamma)  
    assert not torch.any(torch.isnan(u0_))
    assert not torch.any(torch.isnan(s0_))
    tau_off = nn.functional.leaky_relu(t-ts, negative_slope=neg_slope) if train_mode else nn.functional.relu(t-ts) 
    expb, expg = torch.exp(-beta*tau_off), torch.exp(-gamma*tau_off)
    uhat_off = u0_*expb
    shat_off = s0_*expg+(-beta*u0_)/(gamma-beta+eps)*(expg-expb) * (1-unstability)
    
    assert not torch.any(torch.isnan(uhat_off))
    assert not torch.any(torch.isnan(shat_off))
    return (uhat_on*o + uhat_off*(1-o)),(shat_on*o + shat_off*(1-o)) 
    


############################################################
#Branching VAE
############################################################
"""
Reinitialization using the estimated global cell time
"""
def transitionTimeRec(t_trans, ts, t_type, prev_type, graph, G, dt_min=0.01):
    """
    Applied to the branching ODE
    Recursive helper function for transition time initialization
    """
    if(len(graph[prev_type])==0):
        return
    for cur_type in graph[prev_type]:
        t_trans[cur_type] = np.quantile(t_type[cur_type],0.01)
        ts[cur_type] = np.clip(np.quantile(t_type[cur_type],0.02) - t_trans[cur_type], a_min=dt_min, a_max=None)
        transitionTimeRec(t_trans, ts, t_type, cur_type, graph, G)
        t_trans[cur_type] = np.clip(t_trans[cur_type]-t_trans[prev_type], a_min=dt_min, a_max=None)
    return

def transitionTime(t, cell_labels, cell_types, graph, init_type, G, dt_min=1e-4):
    """
    Applied to the branching ODE
    Initialize transition (between consecutive cell types) and switching(within the same cell type) time.
    """
    t_type = {}
    for i, type_ in enumerate(cell_types):
        t_type[type_] = t[cell_labels==type_]
    ts = np.zeros((len(cell_types),G))
    t_trans = np.zeros((len(cell_types)))
    for x in init_type:
        t_trans[x] = t_type[x].min()
        ts[x] = np.clip(np.quantile(t_type[x],0.01) - t_trans[x], a_min=dt_min, a_max=None)
        transitionTimeRec(t_trans, ts, t_type, x, graph, G, dt_min=dt_min)
    return t_trans, ts



def linregMtx(u,s):
    """
    Performs linear regression ||U-kS||_2 while 
    U and S are matrices and k is a vector.
    Handles divide by zero by returninig some default value.
    """
    Q = np.sum(s*s, axis=0)
    R = np.sum(u*s, axis=0)
    k = R/Q
    if np.isinf(k) or np.isnan(k):
        k = 1.5
    #k[np.isinf(k) | np.isnan(k)] = 1.5
    return k

def reinitTypeParams(U, S, t, ts, cell_labels, cell_types, init_types):
    """
    Applied under branching ODE
    Use the steady-state model and estimated cell time to initialize
    branching ODE parameters.
    """
    Ntype = len(cell_types)
    G = U.shape[1]
    alpha, beta, gamma = np.ones((Ntype,G)), np.ones((Ntype,G)), np.ones((Ntype,G))
    u0, s0 = np.zeros((len(init_types),G)), np.zeros((len(init_types),G))
    #sigma_u, sigma_s = np.zeros((Ntype,G)), np.zeros((Ntype,G))
    
    for i, type_ in enumerate(cell_types):
        mask_type = cell_labels == type_
        #Determine induction or repression
        
        t_head = np.quantile(t[mask_type],0.02)
        t_mid = (t_head+np.quantile(t[mask_type],0.98))*0.5
    
        u_head = np.mean(U[(t>=t[mask_type].min()) & (t<t_head),:],axis=0)
        u_mid = np.mean(U[(t>=t_mid*0.98) & (t<=t_mid*1.02),:],axis=0)
    
        s_head = np.mean(S[(t>=t[mask_type].min()) & (t<t_head),:],axis=0)
        s_mid = np.mean(S[(t>=t_mid*0.98) & (t<=t_mid*1.02),:],axis=0)
    
        o = u_head + s_head < u_mid + s_mid
        
        #Determine ODE parameters
        U_type, S_type = U[(cell_labels==type_)], S[(cell_labels==type_)]
        
        for g in range(G):
            u_low = np.min(U_type[:,g])
            s_low = np.min(S_type[:,g])
            u_high = np.quantile(U_type[:,g],0.93)
            s_high = np.quantile(S_type[:,g],0.93)
            mask_high =  (U_type[:,g]>u_high) | (S_type[:,g]>s_high)
            mask_low = (U_type[:,g]<u_low) | (S_type[:,g]<s_low)
            mask_q = mask_high | mask_low
            u_q = U_type[mask_q,g]
            s_q = S_type[mask_q,g]
            slope = linregMtx(u_q-U_type[:,g].min(), s_q-S_type[:,g].min())
            if(slope == 1):
                slope = 1 + 0.1*np.random.rand()
            gamma[type_, g] = np.clip(slope, 0.01, None)
        
        alpha[type_] = (np.quantile(U_type,0.93,axis=0) - np.quantile(U_type,0.07,axis=0)) * o \
                        + (np.quantile(U_type,0.93,axis=0) - np.quantile(U_type,0.07,axis=0)) * (1-o) * np.random.rand(G) * 0.001+1e-10
            
            
    for i, type_ in enumerate(init_types):
        mask_type = cell_labels == type_
        t_head = np.quantile(t[mask_type],0.03)
        u0[i] = np.mean(U[(t>=t[mask_type].min()) & (t<=t_head)],axis=0)+1e-10
        s0[i] = np.mean(S[(t>=t[mask_type].min()) & (t<=t_head)],axis=0)+1e-10
        
     
    return alpha,beta,gamma,u0,s0

def recoverTransitionTimeRec(t_trans, ts, prev_type, graph):
    """
    Recursive helper function of recovering transition time.
    """
    if(len(graph[prev_type])==0):
        return
    for cur_type in graph[prev_type]:
        t_trans[cur_type] += t_trans[prev_type]
        ts[cur_type] += t_trans[cur_type]
        recoverTransitionTimeRec(t_trans, ts, cur_type, graph)
    return

def recoverTransitionTime(t_trans, ts, graph, init_type):
    """
    Recovers the transition and switching time from the relative time.
    
    t_trans: [N type] transition time of each cell type
    ts: [N type x G] switch-time of each gene in each cell type
    graph: (dictionary) transition graph
    init_type: (list) initial cell types
    """
    t_trans_orig = deepcopy(t_trans) if isinstance(t_trans,np.ndarray) else t_trans.clone()
    ts_orig = deepcopy(ts) if isinstance(ts, np.ndarray) else ts.clone()
    for x in init_type:
        ts_orig[x] += t_trans_orig[x]
        recoverTransitionTimeRec(t_trans_orig, ts_orig, x, graph)
    return t_trans_orig, ts_orig

def odeInitialRec(U0, S0, t_trans, ts, prev_type, graph, init_type, use_numpy=False, **kwargs):
    """
    Recursive Helper Function to Compute the Initial Conditions
    1. U0, S0: stores the output, passed by reference
    2. t_trans: [N type] Transition time (starting time)
    3. ts: [N_type x 2 x G] Switching time (Notice that there are 2 phases and the time is gene-specific)
    4. prev_type: previous cell type (parent)
    5. graph: dictionary representation of the transition graph
    6. init_type: starting cell types
    """
    alpha,beta,gamma = kwargs['alpha'][prev_type], kwargs['beta'][prev_type], kwargs['gamma'][prev_type]
    u0, s0 = U0[prev_type], S0[prev_type]

    for cur_type in graph[prev_type]:
        if(use_numpy):
            u0_cur, s0_cur = predSUNumpy(np.clip(t_trans[cur_type]-ts[prev_type],0,None), u0, s0, alpha, beta, gamma)
        else:
            u0_cur, s0_cur = predSU(nn.functional.relu(t_trans[cur_type]-ts[prev_type]), u0, s0, alpha, beta, gamma)
        U0[cur_type] = u0_cur
        S0[cur_type] = s0_cur

        odeInitialRec(U0, 
                      S0, 
                      t_trans, 
                      ts, 
                      cur_type, 
                      graph, 
                      init_type,
                      use_numpy=use_numpy,
                      **kwargs)
                      
    return

def odeInitial(t_trans, 
               ts, 
               graph, 
               init_type, 
               alpha, 
               beta, 
               gamma,
               u0,
               s0,
               use_numpy=False):
    """
    Traverse the transition graph to compute the initial conditions of all cell types.
    """
    U0, S0 = {}, {}
    for i,x in enumerate(init_type):
        U0[x] = u0[i]
        S0[x] = s0[i]
        odeInitialRec(U0,
                      S0,
                      t_trans,
                      ts,
                      x,
                      graph,
                      init_type,
                      alpha=alpha,
                      beta=beta,
                      gamma=gamma,
                      use_numpy=use_numpy)
    return U0, S0

def odeBranch(t, graph, init_type, use_numpy=False, train_mode=False, neg_slope=1e-4, **kwargs):
    """
    Top-level function to compute branching ODE solution
    
    t: [B x 1] cell time
    graph: transition graph
    init_type: initial cell types
    use_numpy: (bool) whether to use the numpy version
    train_mode: (bool) affects whether to use the leaky ReLU to train the transition and switch-on time.
    """
    alpha,beta,gamma = kwargs['alpha'], kwargs['beta'], kwargs['gamma'] #[N type x G]
    t_trans, ts = kwargs['t_trans'], kwargs['ts']
    u0,s0 = kwargs['u0'], kwargs['s0']
    if(use_numpy):
        U0, S0 = np.zeros((alpha.shape[0], alpha.shape[-1])), np.zeros((alpha.shape[0], alpha.shape[-1]))
    else:
        U0, S0 = torch.empty(alpha.shape[0], alpha.shape[-1]).to(t.device), torch.empty(alpha.shape[0], alpha.shape[-1]).to(t.device)
    #Compute initial states of all cell types and genes
    U0_dic, S0_dic = odeInitial(t_trans,
                       ts,
                       graph,
                       init_type,
                       alpha,
                       beta,
                       gamma,
                       u0,
                       s0,
                       use_numpy=use_numpy)
    for i in U0_dic:
        U0[i] = U0_dic[i]
        S0[i] = S0_dic[i]
    
    #Recover the transition time
    t_trans_orig, ts_orig = recoverTransitionTime(t_trans, ts, graph, init_type) 

    #Compute ode solution
    if(use_numpy):
        tau = np.clip(t.reshape(-1,1,1)-ts_orig,0,None)
        uhat, shat = predSUNumpy(tau, U0, S0, alpha, beta, gamma)
    else:
        tau = nn.functional.leaky_relu(t.unsqueeze(-1)-ts_orig, negative_slope=neg_slope) if train_mode else nn.functional.relu(t.unsqueeze(-1)-ts_orig)
        uhat, shat = predSU(tau, U0, S0, alpha, beta, gamma)
    
    return uhat, shat


def odeBranchNumpy(t, graph, init_type, **kwargs):
    """
    (Numpy Version)
    Top-level function to compute branching ODE solution. Wraps around odeBranch
    """
    Ntype = len(graph.keys())
    cell_labels = kwargs['cell_labels']
    scaling = kwargs['scaling']
    py = np.zeros((t.shape[0], Ntype, 1))
    for i in range(Ntype):
        py[cell_labels==i,i,:] = 1
    uhat, shat = odeBranch(t, graph, init_type, True, **kwargs)
    return np.sum(py*(uhat*scaling), 1), np.sum(py*shat, 1)


    

############################################################
#Proposed new method that considers all possible cell type
# transitions. The initial condition is a weighted sum
# of ODEs, where the weight represents transition probability.
############################################################
def initAllPairs(alpha,
                 beta,
                 gamma,
                 t_trans,
                 ts,
                 u0,
                 s0,
                 train_mode=False,
                 neg_slope=1e-4):
    """
    Notice: t_trans and ts are all the absolute values, not relative values
    """
    Ntype = alpha.shape[0]
    G = alpha.shape[1]
    
    #Compute different initial conditions
    tau0 = F.leaky_relu(t_trans.view(-1,1,1) - ts, neg_slope) if train_mode else F.relu(t_trans.view(-1,1,1) - ts)
    U0_hat, S0_hat = predSU(tau0, u0, s0, alpha, beta, gamma) #initial condition of the current type considering all possible parent types
    
    return F.relu(U0_hat), F.relu(S0_hat)

def computeMixWeight(mu_t, sigma_t,
                     cell_labels,
                     alpha,
                     beta,
                     gamma,
                     t_trans,
                     t_end,
                     ts,
                     u0,
                     s0,
                     sigma_u,
                     sigma_s,
                     eps_t,
                     k=1):
    
    U0_hat, S0_hat = initAllPairs(alpha,
                                  beta,
                                  gamma,
                                  t_trans,
                                  ts,
                                  u0,
                                  s0,
                                  False)
    
    Ntype = alpha.shape[0]
    var = torch.mean(sigma_u.pow(2)+sigma_s.pow(2))
    
    tscore = torch.empty(Ntype, Ntype).to(alpha.device)
    
    mu_t_type = [mu_t[cell_labels==i] for i in range(Ntype)]
    std_t_type = [sigma_t[cell_labels==i] for i in range(Ntype)]
    for i in range(Ntype):#child
        for j in range(Ntype):#parent
            mask1, mask2 = (mu_t_type[j]<t_trans[i]-3*eps_t).float(), (mu_t_type[j]>=t_trans[i]+3*eps_t).float()
            tscore[i, j] = torch.mean( ((mu_t_type[j]-t_trans[i]).pow(2) + (std_t_type[j] - eps_t).pow(2))*(mask1+mask2*k) )
    
    xscore = torch.mean(((U0_hat-u0.unsqueeze(1))).pow(2)+((S0_hat-s0.unsqueeze(1))).pow(2),-1) + torch.eye(alpha.shape[0]).to(alpha.device)*var*0.1
    
    #tmask = t_trans.view(-1,1)<t_trans
    #xscore[tmask] = var*1e3
    mu_tscore, mu_xscore = tscore.mean(), xscore.mean()
    logit_w = - tscore/mu_tscore - xscore/mu_xscore
    
    return logit_w, tscore, xscore



def odeWeighted(t, y_onehot, train_mode=False, neg_slope=0, **kwargs):
    """
    Compute the ODE solution given every possible parent cell type
    """
    alpha,beta,gamma = kwargs['alpha'], kwargs['beta'], kwargs['gamma'] #[N type x G]
    t_trans, ts = kwargs['t_trans'], kwargs['ts']
    u0,s0 = kwargs['u0'], kwargs['s0'] #[N type x G]
    sigma_u = kwargs['sigma_u']
    sigma_s = kwargs['sigma_s']
    scaling=kwargs["scaling"]
    
    Ntype, G = alpha.shape
    N = y_onehot.shape[0]
    
    U0_hat, S0_hat = initAllPairs(alpha,
                                  beta,
                                  gamma,
                                  t_trans,
                                  ts,
                                  u0,
                                  s0,
                                  train_mode,
                                  neg_slope) #(type, parent type, gene)
    
    tau = F.leaky_relu( t.view(N,1,1,1) - ts.view(Ntype,1,G), neg_slope) if train_mode else F.relu( t.view(N,1,1,1) - ts.view(Ntype,1,G) ) #(cell, type, parent type, gene)
    Uhat, Shat = predSU(tau,
                        U0_hat,
                        S0_hat,
                        alpha.view(Ntype, 1, G),
                        beta.view(Ntype, 1, G),
                        gamma.view(Ntype, 1, G))
    
    return ((Uhat*y_onehot.view(N,Ntype,1,1)).sum(1))*scaling, (Shat*y_onehot.view(N,Ntype,1,1)).sum(1)

def odeWeightedNumpy(t, y_onehot, w_onehot, **kwargs):
    alpha,beta,gamma = kwargs['alpha'], kwargs['beta'], kwargs['gamma'] #[N type x G]
    t_trans, ts = kwargs['t_trans'], kwargs['ts']
    u0,s0 = kwargs['u0'], kwargs['s0'] #[N type x G]
    scaling=kwargs["scaling"]
    
    Ntype, G = alpha.shape
    N = y_onehot.shape[0]
    
    #Compute different initial conditions
    tau0 = np.clip(t_trans.reshape(-1,1,1) - ts, 0, None)
    U0_hat, S0_hat = predSUNumpy(tau0, u0, s0, alpha, beta, gamma) #initial condition of the current type considering all possible parent types
    
    tau = np.clip( t.reshape(t.shape[0],1,1,1) - ts.reshape(Ntype,1,G), 0, None) #(cell, type, gene)
    Uhat, Shat = predSUNumpy(tau,
                             U0_hat,
                             S0_hat, 
                             alpha.reshape(Ntype, 1, G),
                             beta.reshape(Ntype, 1, G),
                             gamma.reshape(Ntype, 1, G))
    Uhat, Shat = np.sum(Uhat*y_onehot.reshape(N, Ntype, 1, 1), 1), np.sum(Shat*y_onehot.reshape(N, Ntype, 1, 1), 1)
    Uhat, Shat = np.sum(Uhat*w_onehot.reshape(N, Ntype, 1), 1), np.sum(Shat*w_onehot.reshape(N, Ntype, 1), 1)
    
    return Uhat, Shat



############################################################
#  Optimal Transport
############################################################


"""
Geoffrey Schiebinger, Jian Shu, Marcin Tabaka, Brian Cleary, Vidya Subramanian, 
  Aryeh Solomon, Joshua Gould, Siyan Liu, Stacie Lin, Peter Berube, Lia Lee, 
  Jenny Chen, Justin Brumbaugh, Philippe Rigollet, Konrad Hochedlinger, Rudolf Jaenisch, Aviv Regev, Eric S. Lander,
  Optimal-Transport Analysis of Single-Cell Gene Expression Identifies Developmental Trajectories in Reprogramming,
  Cell,
  Volume 176, Issue 4,
  2019,
  Pages 928-943.e22,
  ISSN 0092-8674,
  https://doi.org/10.1016/j.cell.2019.01.006.
"""
# @ Lénaïc Chizat 2015 - optimal transport
def fdiv(l, x, p, dx):
    return l * np.sum(dx * (x * (np.log(x / p)) - x + p))


def fdivstar(l, u, p, dx):
    return l * np.sum((p * dx) * (np.exp(u / l) - 1))


def primal(C, K, R, dx, dy, p, q, a, b, epsilon, lambda1, lambda2):
    I = len(p)
    J = len(q)
    F1 = lambda x, y: fdiv(lambda1, x, p, y)
    F2 = lambda x, y: fdiv(lambda2, x, q, y)
    with np.errstate(divide='ignore'):
        return F1(np.dot(R, dy), dx) + F2(np.dot(R.T, dx), dy) \
               + (epsilon * np.sum(R * np.nan_to_num(np.log(R)) - R + K) \
                  + np.sum(R * C)) / (I * J)


def dual(C, K, R, dx, dy, p, q, a, b, epsilon, lambda1, lambda2):
    I = len(p)
    J = len(q)
    F1c = lambda u, v: fdivstar(lambda1, u, p, v)
    F2c = lambda u, v: fdivstar(lambda2, u, q, v)
    return - F1c(- epsilon * np.log(a), dx) - F2c(- epsilon * np.log(b), dy) \
           - epsilon * np.sum(R - K) / (I * J)


# end @ Lénaïc Chizat

def optimal_transport_duality_gap(C, G, lambda1, lambda2, epsilon, batch_size, tolerance, tau,
                                  epsilon0, max_iter, **ignored):
    """
    Compute the optimal transport with stabilized numerics, with the guarantee that the duality gap is at most `tolerance`
    Parameters
    ----------
    C : 2-D ndarray
        The cost matrix. C[i][j] is the cost to transport cell i to cell j
    G : 1-D array_like
        Growth value for input cells.
    lambda1 : float, optional
        Regularization parameter for the marginal constraint on p
    lambda2 : float, optional
        Regularization parameter for the marginal constraint on q
    epsilon : float, optional
        Entropy regularization parameter.
    batch_size : int, optional
        Number of iterations to perform between each duality gap check
    tolerance : float, optional
        Upper bound on the duality gap that the resulting transport map must guarantee.
    tau : float, optional
        Threshold at which to perform numerical stabilization
    epsilon0 : float, optional
        Starting value for exponentially-decreasing epsilon
    max_iter : int, optional
        Maximum number of iterations. Print a warning and return if it is reached, even without convergence.
    Returns
    -------
    transport_map : 2-D ndarray
        The entropy-regularized unbalanced transport map
    """
    C = np.asarray(C, dtype=np.float64)
    epsilon_scalings = 5
    scale_factor = np.exp(- np.log(epsilon) / epsilon_scalings)

    I, J = C.shape
    dx, dy = np.ones(I) / I, np.ones(J) / J

    p = G
    q = np.ones(C.shape[1]) * np.average(G)

    u, v = np.zeros(I), np.zeros(J)
    a, b = np.ones(I), np.ones(J)

    epsilon_i = epsilon0 * scale_factor
    current_iter = 0

    for e in range(epsilon_scalings + 1):
        duality_gap = np.inf
        u = u + epsilon_i * np.log(a)
        v = v + epsilon_i * np.log(b)  # absorb
        epsilon_i = epsilon_i / scale_factor
        _K = np.exp(-C / epsilon_i)
        alpha1 = lambda1 / (lambda1 + epsilon_i)
        alpha2 = lambda2 / (lambda2 + epsilon_i)
        K = np.exp((np.array([u]).T - C + np.array([v])) / epsilon_i)
        a, b = np.ones(I), np.ones(J)
        old_a, old_b = a, b
        threshold = tolerance if e == epsilon_scalings else 1e-6

        while duality_gap > threshold:
            for i in range(batch_size if e == epsilon_scalings else 5):
                current_iter += 1
                old_a, old_b = a, b
                a = (p / (K.dot(np.multiply(b, dy)))) ** alpha1 * np.exp(-u / (lambda1 + epsilon_i))
                b = (q / (K.T.dot(np.multiply(a, dx)))) ** alpha2 * np.exp(-v / (lambda2 + epsilon_i))

                # stabilization
                if (max(max(abs(a)), max(abs(b))) > tau):
                    u = u + epsilon_i * np.log(a)
                    v = v + epsilon_i * np.log(b)  # absorb
                    K = np.exp((np.array([u]).T - C + np.array([v])) / epsilon_i)
                    a, b = np.ones(I), np.ones(J)

                if current_iter >= max_iter:
                    logger.warning("Reached max_iter with duality gap still above threshold. Returning")
                    return (K.T * a).T * b

            # The real dual variables. a and b are only the stabilized variables
            _a = a * np.exp(u / epsilon_i)
            _b = b * np.exp(v / epsilon_i)

            # Skip duality gap computation for the first epsilon scalings, use dual variables evolution instead
            if e == epsilon_scalings:
                R = (K.T * a).T * b
                pri = primal(C, _K, R, dx, dy, p, q, _a, _b, epsilon_i, lambda1, lambda2)
                dua = dual(C, _K, R, dx, dy, p, q, _a, _b, epsilon_i, lambda1, lambda2)
                duality_gap = (pri - dua) / abs(pri)
            else:
                duality_gap = max(
                    np.linalg.norm(_a - old_a * np.exp(u / epsilon_i)) / (1 + np.linalg.norm(_a)),
                    np.linalg.norm(_b - old_b * np.exp(v / epsilon_i)) / (1 + np.linalg.norm(_b)))

    if np.isnan(duality_gap):
        raise RuntimeError("Overflow encountered in duality gap computation, please report this incident")
    return R / C.shape[1]


############################################################
#Other Auxilliary Functions
############################################################
def makeDir(file_path):
    directories = file_path.split('/')
    cur_path = ''
    for directory in directories:
        cur_path += directory
        if(directory=='.' or directory == '..' or directory==''):
            continue
        if not os.path.exists(cur_path):
            os.mkdir(cur_path)
        cur_path += '/'

def getGeneIndex(genes_all, gene_list):
    gind = []
    gremove = []
    for gene in gene_list:
        matches = np.where(genes_all==gene)[0]
        if(len(matches)==1):
            gind.append(matches[0])
        elif(len(matches)==0):
            print(f'Warning: Gene {gene} not found! Ignored.')
            gremove.append(gene)
        else:
            gind.append(matches[0])
            print('Warning: Gene {gene} has multiple matches. Pick the first one.')
    for gene in gremove:
        gene_list.remove(gene)
    return gind, gene_list
    
def convertTime(t):
    """
    Convert the time in sec into the format: hour:minute:second
    """
    hour = int(t//3600)
    minute = int((t - hour*3600)//60)
    second = int(t - hour*3600 - minute*60)
    
    return f"{hour:3d} h : {minute:2d} m : {second:2d} s"