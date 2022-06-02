import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import os
import time
from velovae.plotting import plot_phase, plot_sig, plot_time, plot_train_loss, plot_test_loss

from .model_util import histEqual, initParams, getTsGlobal, reinitParams, convertTime, ode, getGeneIndex
from .TrainingData import SCData
from .velocity import rnaVelocityVanillaVAE


############################################################
#KL Divergence
############################################################
def kl_uniform(mu_t, std_t, t_start, t_end, **kwargs):
    """
    < Description >
    KL Divergence for the 1D near-uniform model
    KL(q||p) where
    q = uniform(t0, t0+dt)
    p = uniform(t_start, t_end) with exponential decays on both sides
    """
    tail = kwargs["tail"] if "tail" in kwargs else 0.05
    t0 = mu_t - np.sqrt(3)*std_t
    dt = np.sqrt(12)*std_t
    C = 1/((t_end-t_start)*(1+tail))
    lamb = 2/(tail*(t_end-t_start))
    
    t1 = t0+dt
    dt1_til = nn.functional.relu(torch.minimum(t_start, t1) - t0)
    dt2_til = nn.functional.relu(t1 - torch.maximum(t_end, t0))
    
    term1 = -lamb*(dt1_til.pow(2)+dt2_til.pow(2))/(2*dt)
    term2 = lamb*((t_start-t0)*dt1_til+(t1-t_end)*dt2_til)/dt
    
    return torch.mean(term1 + term2 - torch.log(C*dt))

def kl_gaussian(mu1, std1, mu2, std2, **kwargs):
    """
    < Description >
    Compute the KL divergence between two Gaussian distributions with diagonal covariance
    """
    return torch.mean(torch.sum(torch.log(std2/std1)+std1.pow(2)/(2*std2.pow(2))-0.5+(mu1-mu2).pow(2)/(2*std2.pow(2)),1))





##############################################################
# Vanilla VAE
##############################################################
class encoder(nn.Module):
    """
    Encoder of the vanilla VAE
    """
    def __init__(self, Cin, N1=500, N2=250, device=torch.device('cpu'), checkpoint=None):
        """
        < Description >
        Constructor of the class
        
        < Input Arguments >
        1.  Cin [int]
            (Optional) Input feature dimension. Usually just 2 x gene count
        
        2.  N1 [int]
            (Optional) Width of the first hidden layer
        
        3.  N2 [int]
            (Optional) Width of the second hidden layer
        
        4.  device [torch.device]
            (Optional) Either cpu or gpu device
        
        5.  checkpoint [string]
            Existing .pt file containing trained parameters
        
        < Output >
        None. Construct an instance of the class.
        """
        super(encoder, self).__init__()
        self.fc1 = nn.Linear(Cin, N1).to(device)
        self.bn1 = nn.BatchNorm1d(num_features=N1).to(device)
        self.dpt1 = nn.Dropout(p=0.2).to(device)
        self.fc2 = nn.Linear(N1, N2).to(device)
        self.bn2 = nn.BatchNorm1d(num_features=N2).to(device)
        self.dpt2 = nn.Dropout(p=0.2).to(device)
        
        self.net = nn.Sequential(self.fc1, self.bn1, nn.LeakyReLU(), self.dpt1,
                                 self.fc2, self.bn2, nn.LeakyReLU(), self.dpt2)
        
        self.fc_mu, self.spt1 = nn.Linear(N2,1).to(device), nn.Softplus()
        self.fc_std, self.spt2 = nn.Linear(N2,1).to(device), nn.Softplus()
        
        if(checkpoint is not None):
            self.load_state_dict(torch.load(checkpoint,map_location=device))
        else:
            self.init_weights()

    def init_weights(self):
        """
        < Description >
        Initialize neural network weights.
        """
        for m in self.net.modules():
            if(isinstance(m, nn.Linear)):
                nn.init.xavier_uniform_(m.weight)
                nn.init.constant_(m.bias, 0.0)
            elif(isinstance(m, nn.BatchNorm1d)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)
        for m in [self.fc_mu, self.fc_std]:
            nn.init.xavier_uniform_(m.weight)
            nn.init.constant_(m.bias, 0.0)

    def forward(self, data_in):
        """
        < Description >
        Forward propagation.
        
        < Input Arguments>
        1.  data_in [float/double tensor]
            Input data of size (N, D) = (Batch Size, Input Data Dimension)
        
        < Output >
        1.  mu_tx [float/double tensor]
            Posterior mean of time, with a size of (N, 1)
            
        2.  std_tx [float/double tensor]
            Posterior standard deviation of time, with a size of (N, 1)
        """
        z = self.net(data_in)
        mu_zx, std_zx = self.spt1(self.fc_mu(z)), self.spt2(self.fc_std(z))
        return mu_zx, std_zx

class decoder(nn.Module):
    def __init__(self, 
                 adata, 
                 tmax, 
                 train_idx, 
                 p=98, 
                 device=torch.device('cpu'), 
                 init_method="steady", 
                 init_key=None):
        """
        < Description >
        Constructor of the class
        
        < Input Arguments >
        1.  adata [AnnData]
            Input AnnData object
        
        2.  tmax [float]
            Time range (hyperparameter)
            
        3.  train_idx [int array]
            The indices of all training samples. We pick 70% of the data as
            training samples by default.
        
        4.  p [int in (0,100)]
            (Optional) Percentile threshold of u and s for picking steady-state cells.
            Used in initialization.
        
        5. device [torch device]
            Either cpu or gpu
        
        6. init_method [string]
            (Optional) Initialization method. 
                        
        7. init_key [string]
            (Optional) column in the AnnData object containing the capture time
        
        < Output >
        None. Construct an instance of the class.
        """
        super(decoder,self).__init__()
        U,S = adata.layers['Mu'][train_idx], adata.layers['Ms'][train_idx]
        X = np.concatenate((U,S),1)
        N,G = U.shape
        #Dynamical Model Parameters
        if(init_method == "random"):
            print("Random Initialization.")
            alpha, beta, gamma, scaling, toff, u0, s0, sigma_u, sigma_s, T, Rscore = initParams(X,p,fit_scaling=True)
            
            self.alpha = nn.Parameter(torch.normal(0.0, 0.01, size=(G,), device=device).float())
            self.beta =  nn.Parameter(torch.normal(0.0, 0.01, size=(G,), device=device).float())
            self.gamma = nn.Parameter(torch.normal(0.0, 0.01, size=(G,), device=device).float())
            self.ton = nn.Parameter(torch.normal(0.0, 0.01, size=(G,), device=device).float())
            self.toff = nn.Parameter(torch.normal(0.0, 0.01, size=(G,), device=device).float()+self.ton.detach())
            self.scaling = nn.Parameter(torch.tensor(np.log(scaling), device=device).float())
            self.sigma_u = nn.Parameter(torch.tensor(np.log(sigma_u), device=device).float())
            self.sigma_s = nn.Parameter(torch.tensor(np.log(sigma_s), device=device).float())
        elif(init_method == "tprior"):
            print("Initialization using prior time.")
            alpha, beta, gamma, scaling, toff, u0, s0, sigma_u, sigma_s, T, Rscore = initParams(X,p,fit_scaling=True)
            t_prior = adata.obs[init_key].to_numpy()
            t_prior = t_prior[train_idx]
            std_t = (np.std(t_prior)+1e-3)*0.2
            self.t_init = np.random.uniform(t_prior-std_t, t_prior+std_t)
            self.t_init -= self.t_init.min()
            self.t_init = self.t_init
            self.t_init = self.t_init/self.t_init.max()*tmax
            toff = getTsGlobal(self.t_init, U/scaling, S, 95)
            alpha, beta, gamma, ton = reinitParams(U/scaling, S, self.t_init, toff)
            
            self.alpha = nn.Parameter(torch.tensor(np.log(alpha), device=device).float())
            self.beta = nn.Parameter(torch.tensor(np.log(beta), device=device).float())
            self.gamma = nn.Parameter(torch.tensor(np.log(gamma), device=device).float())
            self.scaling = nn.Parameter(torch.tensor(np.log(scaling), device=device).float())
            self.ton = nn.Parameter(torch.tensor(np.log(ton+1e-10), device=device).float())
            self.toff = nn.Parameter(torch.tensor(np.log(toff+1e-10), device=device).float())
            self.sigma_u = nn.Parameter(torch.tensor(np.log(sigma_u), device=device).float())
            self.sigma_s = nn.Parameter(torch.tensor(np.log(sigma_s), device=device).float())
        else:
            print("Initialization using the steady-state and dynamical models.")
            alpha, beta, gamma, scaling, toff, u0, s0, sigma_u, sigma_s, T, Rscore = initParams(X,p,fit_scaling=True)
            if(init_key is not None):
                t_init = adata.obs['init_key'].to_numpy()
            else:
                T = T+np.random.rand(T.shape[0],T.shape[1]) * 1e-3
                T_eq = np.zeros(T.shape)
                Nbin = T.shape[0]//50+1
                for i in range(T.shape[1]):
                    T_eq[:, i] = histEqual(T[:, i], tmax, 0.9, Nbin)
                self.t_init = np.quantile(T_eq,0.5,1)
            toff = getTsGlobal(self.t_init, U/scaling, S, 95)
            alpha, beta, gamma,ton = reinitParams(U/scaling, S, self.t_init, toff)
            
            self.alpha = nn.Parameter(torch.tensor(np.log(alpha), device=device).float())
            self.beta = nn.Parameter(torch.tensor(np.log(beta), device=device).float())
            self.gamma = nn.Parameter(torch.tensor(np.log(gamma), device=device).float())
            self.scaling = nn.Parameter(torch.tensor(np.log(scaling), device=device).float())
            self.ton = nn.Parameter(torch.tensor(np.log(ton+1e-10), device=device).float())
            self.toff = nn.Parameter(torch.tensor(np.log(toff+1e-10), device=device).float())
            self.sigma_u = nn.Parameter(torch.tensor(np.log(sigma_u), device=device).float())
            self.sigma_s = nn.Parameter(torch.tensor(np.log(sigma_s), device=device).float())

        self.scaling.requires_grad = False
        self.sigma_u.requires_grad = False
        self.sigma_s.requires_grad = False
    
    def forward(self, t, neg_slope=0.0):
        """
        < Description >
        Forward pass of the decoder. This is equivalent to evaluating the ODE
        solution in our application.
        
        < Input Arguments >
        1.  t [tensor]
            cell time of size (N, 1) = (batch size, 1)
        
        2.  neg_slope [negative float]
            (Optional) Negative slope of the leaky ReLU used in time computation.
            The original purpose was to provide some gradient to the backward
            pass so that samples violating the time relation also contribute to
            the training. Later we found it not helpful to the performance.
            The users should consider leaving it to the default.
        
        < Output >
        1-2. Predicted u and s values [tensor (N,G)]
        """
        Uhat, Shat = ode(t, torch.exp(self.alpha), torch.exp(self.beta), torch.exp(self.gamma), torch.exp(self.ton), torch.exp(self.toff), neg_slope=neg_slope)
        Uhat = Uhat * torch.exp(self.scaling)
        return nn.functional.relu(Uhat), nn.functional.relu(Shat)
    
    def predSU(self, t, gidx=None):
        """
        < Description >
        ODE evaluation.
        This is the same as forward except that we can optionally compute u and s
        for a subset of genes. Used in plotting.
        """
        scaling = torch.exp(self.scaling)
        if(gidx is not None):
            Uhat, Shat = ode(t, torch.exp(self.alpha[gidx]), torch.exp(self.beta[gidx]), torch.exp(self.gamma[gidx]), torch.exp(self.ton[gidx]), torch.exp(self.toff[gidx]), neg_slope=0.0)
            return nn.functional.relu(Uhat*scaling[gidx]), nn.functional.relu(Shat)
        Uhat, Shat = ode(t, torch.exp(self.alpha), torch.exp(self.beta), torch.exp(self.gamma), torch.exp(self.ton), torch.exp(self.toff), neg_slope=0.0)
        return nn.functional.relu(Uhat*scaling), nn.functional.relu(Shat)

class VanillaVAE():
    def __init__(self, 
                 adata, 
                 tmax, 
                 device='cpu', 
                 hidden_size=(500, 250), 
                 init_method="steady",
                 init_key=None,
                 tprior=None, 
                 time_distribution="gaussian",
                 checkpoints=None):
        """
        < Description >
        Constructor of the class
        
        < Input Arguments >
        1.  adata [AnnData Object]
            
        2.  tmax [float/int] 
            Time Range 
        
        3.  device [string]
            (Optional) cpu or gpu
        
        4.  hidden_size [tuple of 2 int]
            (Optional) Width of the first and second hidden layer [Default:(500, 250)]
        
        5.  init_type [string]
            (Optional) The stem cell type. Used to estimated the initial conditions.
            This is not commonly used in practice and please consider leaving it to default.
        
        6.  init_key [string]
            (Optional) column in the AnnData object containing the capture time
        
        7.  tprior [string]
            (Optional) key in adata.obs that stores the capture time.
            Used for informative time prior
        
        8. time_distribution [string]
            (Optional) Should be either "gaussian" or "uniform.
        
        9. checkpoints [list of 2 strings]
            (Optional) Contains the path to saved encoder and decoder models.
            Should be a .pt file.
        """
        #Extract Input Data
        try:
            U,S = adata.layers['Mu'], adata.layers['Ms']
        except KeyError:
            print('Unspliced/Spliced count matrices not found in the layers! Exit the program...')
        
        #Default Training Configuration
        self.config = {
            #Model Parameters
            "tmax":tmax,
            "hidden_size":hidden_size,
            "init_method": init_method,
            "init_key": init_key,
            "tprior":tprior,
            "tail":0.01,
            "time_overlap":0.5,

            #Training Parameters
            "n_epochs":2000, 
            "batch_size":128,
            "learning_rate":2e-4, 
            "learning_rate_ode":5e-4, 
            "lambda":1e-3, 
            "kl_t":1.0, 
            "test_iter":None, 
            "save_epoch":100,
            "n_warmup":5,
            "early_stop":5,
            "early_stop_thred":1e-3*adata.n_vars,
            "train_test_split":0.7,
            "k_alt":1,
            "neg_slope":0.0,
            "train_scaling":False, 
            "train_std":False, 
            "weight_sample":False,
            
            #Plotting
            "sparsify":1
        }
        
        self.setDevice(device)
        self.splitTrainTest(adata.n_obs)
        
        G = adata.n_vars
        #Create an encoder
        try:
            self.encoder = encoder(2*G, hidden_size[0], hidden_size[1], self.device, checkpoint=checkpoints).float()
        except IndexError:
            print('Please provide two dimensions!')
        #Create a decoder
        self.decoder = decoder(adata, 
                               tmax, 
                               self.train_idx, 
                               device=self.device, 
                               init_method = init_method,
                               init_key = init_key).float()
        self.tmax=torch.tensor(tmax).to(self.device)
        self.time_distribution = time_distribution
        #Time prior
        self.getPrior(adata, time_distribution, tmax, tprior)
        
        #class attributes for training
        self.loss_train, self.loss_test = [], []
        self.counter = 0 #Count the number of iterations
        self.n_drop = 0 #Count the number of consecutive epochs with negative/low ELBO gain
        
    def getPrior(self, adata, time_distribution, tmax, tprior=None):
        """
        < Description >
        Compute the parameters of time prior distribution
        
        < Input Argument >
        1.  adata [AnnData]
        
        2.  time_distribution [string]
            "gaussian" or uniform
        
        3.  tmax [float]
            maximum time
        
        4.  tprior [string]
            (Optional) key in adata.obs storing the capture time
        
        < Output >
        None. Initializes class variable p_t
        """
        if(time_distribution=="gaussian"):
            print("Gaussian Prior.")
            self.kl_time = kl_gaussian
            self.sample = self.reparameterize
            if(tprior is None):
                self.p_t = torch.stack([torch.ones(adata.n_obs,1)*tmax*0.5,torch.ones(adata.n_obs,1)*tmax*self.config["time_overlap"]]).float().to(self.device)
            else:
                print('Using informative time prior.')
                t = adata.obs[tprior].to_numpy()
                t = t/t.max()*tmax
                t_cap = np.sort(np.unique(t))
                
                std_t = np.zeros((len(t)))
                std_t[t==t_cap[0]] = (t_cap[1] - t_cap[0])*(0.5+0.5*self.config["time_overlap"])
                for i in range(1, len(t_cap)-1):
                    std_t[t==t_cap[i]] = 0.5*(t_cap[i] - t_cap[i-1])*(0.5+0.5*self.config["time_overlap"]) + 0.5*(t_cap[i+1] - t_cap[i])*(0.5+0.5*self.config["time_overlap"])
                std_t[t==t_cap[-1]] = (t_cap[-1] - t_cap[-2])*(0.5+0.5*self.config["time_overlap"])
                
                self.p_t = torch.stack( [torch.tensor(t).view(-1,1),torch.tensor(std_t).view(-1,1)] ).float().to(self.device)
        else:
            print("Tailed Uniform Prior.")
            self.kl_time = kl_uniform
            self.sample = self.reparameterize_uniform
            if(tprior is None):
                self.p_t = torch.stack([torch.zeros(adata.n_obs,1),torch.ones(adata.n_obs,1)*tmax]).float().to(self.device)
            else:
                print('Using informative time prior.')
                t = adata.obs[tprior].to_numpy()
                t = t/t.max()*tmax
                t_cap = np.sort(np.unique(t))
                t_start = np.zeros((len(t)))
                t_end = np.zeros((len(t)))
                for i in range(len(t_cap)-1):
                    t_end[t==t_cap[i]] = t_cap[i] + (t_cap[i+1] - t_cap[i])*(0.5+0.5*self.config["time_overlap"])
                t_end[t==t_cap[-1]] = t_cap[-1] + (t_cap[-1] - t_cap[-2])*(0.5+0.5*self.config["time_overlap"])
                
                for i in range(1, len(t_cap)):
                    t_start[t==t_cap[i]] = max(0, t_cap[i] - (t_cap[i] - t_cap[i-1])*(0.5+0.5*self.config["time_overlap"]))
                t_start[t==t_cap[0]] = max(0, t_cap[0] - (t_cap[1] - t_cap[0])*(0.5+0.5*self.config["time_overlap"]))
                
                self.p_t = torch.stack( [torch.tensor(t).unsqueeze(-1),torch.tensor(t_end).unsqueeze(-1)] ).float().to(self.device)
    
    def setDevice(self, device, device_number=None):
        """
        < Description >
        Set the device of the model.
        """
        if('cuda' in device):
            if(torch.cuda.is_available()):
                self.device = torch.device(device)
            else:
                print('Warning: GPU not detected. Using CPU as the device.')
                self.device = torch.device('cpu')
        else:
            self.device = torch.device('cpu')
    
    def reparameterize(self, mu, std):
        """
        < Description >
        Apply the reparameterization trick for Gaussian random variables.
        """
        eps = torch.normal(mean=torch.zeros(mu.shape),std=torch.ones(mu.shape)).to(self.device)
        return std*eps+mu
    
    def reparameterize_uniform(self, mu, std):
        """
        < Description >
        Apply the reparameterization trick for uniform random variables.
        """
        eps = torch.rand(mu.shape).to(self.device)
        return np.sqrt(12)*std*eps + (mu - np.sqrt(3)*std)
    
    def forward(self, data_in):
        data_in_scale = torch.cat((data_in[:,:data_in.shape[1]//2]/torch.exp(self.decoder.scaling), data_in[:,data_in.shape[1]//2:]),1)
        mu_t, std_t = self.encoder.forward(data_in_scale)
        t_global = self.reparameterize(mu_t, std_t)
         
        uhat, shat = self.decoder.forward(t_global, neg_slope=self.config["neg_slope"]) #uhat is scaled
        return mu_t, std_t, t_global, uhat, shat
    
    def evalModel(self, data_in):
        data_in_scale = torch.cat((data_in[:,:data_in.shape[1]//2]/torch.exp(self.decoder.scaling), data_in[:,data_in.shape[1]//2:]),1)
        mu_t, std_t = self.encoder.forward(data_in_scale)
        
        uhat, shat = self.decoder.predSU(mu_t) #uhat is scaled
        return mu_t, std_t, uhat, shat
        
    def setMode(self,mode):
        """
        < Description >
        Set the model to either training or evaluation mode.
        """
        if(mode=='train'):
            self.encoder.train()
            self.decoder.train()
        elif(mode=='eval'):
            self.encoder.eval()
            self.decoder.eval()
        else:
            print("Warning: mode not recognized. Must be 'train' or 'test'! ")
    
    ############################################################
    #Training Objective
    ############################################################
    def VAERisk(self, q_tx, p_t, u, s, uhat, shat, sigma_u, sigma_s, weight=None, b=1.0):
        """
        < Description >
        This is the negative ELBO.
        
        < Input Arguments >
        1.  q_tx [a tuple of tensors (mean, standard deviation)]
            Parameters of time posterior. Mean and std are both (N, 1) tensors.
        
        2.  p_t [a tuple of tensors (mean, standard deviation)]
            Parameters of time prior.
        
        5.  u , s [tensor (B,G)]
            Input data
        
        6.  uhat, shat [tensor (B,G)]
            Prediction by VeloVAE
        
        7.  sigma_u, sigma_s [tensor (G)]
            Standard deviation of the Gaussian noise
        
        8.  weight [tensor (N,1)]
            (Optional) Sample weight. 
            This feature is not stable. Please consider setting it to None.
        
        9.  b [float]
            KL divergence weight
        
        < Output >
        1.  Negative ELBO [tensor scalar]
        """
        
        kldt = self.kl_time(q_tx[0], q_tx[1], p_t[0], p_t[1], tail=self.config["tail"])
        
        #u and sigma_u has the original scale
        logp = -0.5*((u-uhat)/sigma_u).pow(2)-0.5*((s-shat)/sigma_s).pow(2)-torch.log(sigma_u)-torch.log(sigma_s*2*np.pi)
        
        if( weight is not None):
            logp = logp*weight
        err_rec = torch.mean(torch.sum(logp,1))
        
        return (- err_rec + b*(kldt))
        
    def train_epoch(self, train_loader, test_set, optimizer, optimizer2=None, K=1):
        """
        < Description >
        Training in each epoch.
        Early stopping if enforced by default. 
        
        < Input Arguments >
        1.  train_loader [torch.utils.data.DataLoader]
            Data loader of the input data.
        
        2.  test_set [torch.utils.data.Dataset]
            Validation dataset
        
        3.  optimizer  [optimizer from torch.optim]
        
        4.  optimizer2 [optimizer from torch.optim]
            (Optional) A second optimizer.
            This is used when we optimize NN and ODE simultaneously in one epoch.
            By default, VeloVAE performs alternating optimization in each epoch.
            The argument will be set to proper value automatically.
        
        5.  K [int]
            Alternating update period.
            For every K updates of optimizer, there's one update for optimizer2.
            If set to 0, optimizer2 will be ignored and only optimizer will be
            updated. Users can set it to 0 if they want to update sorely NN in one 
            epoch and ODE in the next epoch. 
        
        < Output >
        1.  stop_training [bool]
            Whether to stop training based on the early stopping criterium.
        """
        B = len(train_loader)
        self.setMode('train')
        stop_training = False
        
        for i, batch in enumerate(train_loader):
            if( self.counter==1 or self.counter % self.config["test_iter"] == 0):
                elbo_test = self.test(test_set, None, self.counter)
                if(len(self.loss_test)>0):
                    if(elbo_test - self.loss_test[-1] <= self.config["early_stop_thred"]):
                        self.n_drop = self.n_drop+1
                    else:
                        self.n_drop = 0
                self.loss_test.append(elbo_test)
                self.setMode('train')
                if(self.n_drop>=self.config["early_stop"] and self.config["early_stop"]>0):
                    stop_training=True
                    break
            
            optimizer.zero_grad()
            if(optimizer2 is not None):
                optimizer2.zero_grad()
            
            xbatch, weight, idx = batch[0].float().to(self.device), batch[2].float().to(self.device), batch[3]
            u = xbatch[:,:xbatch.shape[1]//2]
            s = xbatch[:,xbatch.shape[1]//2:]
            mu_tx, std_tx, t_global, uhat, shat = self.forward(xbatch)
            
            loss = self.VAERisk((mu_tx,std_tx), 
                                self.p_t[:,self.train_idx[idx],:], 
                                u, s, 
                                uhat, shat, 
                                torch.exp(self.decoder.sigma_u), torch.exp(self.decoder.sigma_s), 
                                None,
                                self.config["kl_t"])
            
            loss.backward()
            if(K==0):
                optimizer.step()
                if( optimizer2 is not None ):
                    optimizer2.step()
            else:
                if( optimizer2 is not None and ((i+1) % (K+1) == 0 or i==B-1)):
                    optimizer2.step()
                else:
                    optimizer.step()
            
            self.loss_train.append(loss.detach().cpu().item())
            self.counter = self.counter + 1
        return stop_training
    
    def loadConfig(self, config):
        """
        
        Update hyper-parameters
        We don't have to specify all the hyperparameters. Just pass the ones we want to modify.
        """
        for key in config:
            if(key in self.config):
                self.config[key] = config[key]
            else:
                self.config[key] = config[key]
                print(f"Warning: unknown hyperparameter: {key}")
        if(self.config["train_scaling"]):
            self.decoder.scaling.requires_grad = True
        if(self.config["train_std"]):
            self.decoder.sigma_u.requires_grad = True
            self.decoder.sigma_s.requires_grad = True
    
    def splitTrainTest(self, N):
        """
        < Description >
        Randomly select indices as training samples.
        """
        rand_perm = np.random.permutation(N)
        n_train = int(N*self.config["train_test_split"])
        self.train_idx = rand_perm[:n_train]
        self.test_idx = rand_perm[n_train:]
        
        return
    
    def train(self, 
              adata, 
              config={}, 
              plot=False, 
              gene_plot=[], 
              cluster_key="clusters",
              figure_path="figures", 
              embed="umap"):
        """
        < Description >
        The high-level API for training.
        
        < Input Arguments >
        1.  adata [AnnData]
            AnnData Object
        
        2.  config [dictionary]
            (Optional) Contains all hyper-parameters.
        
        3.  plot [bool]
            (Optional) Whether to plot some sample genes during training.
            Used for debugging.
        
        4.  gene_plot [list of strings]
            (Optional) List of gene names to plot. 
            Used only if plot==True
        
        5.  cluster_key [string]
            (Optional) Key in adata.obs storing the cell type annotation.
         
        6.  figure_path [string]
            (Optional) Path to the folder for saving plots.
        
        7.  embed [string]
            (Optional) Low dimensional embedding in adata.obsm.
            The actual key storing the embedding should be f'X_{embed}'
        
        < Output >
        None
        """
        self.loadConfig(config)
        
        print("------------------------- Train a Vanilla VAE -------------------------")
        #Get data loader
        U,S = adata.layers['Mu'], adata.layers['Ms']
        X = np.concatenate((U,S), 1)
        try:
            Xembed = adata.obsm[f"X_{embed}"]
        except KeyError:
            print("Embedding not found! Please run the corresponding preprocessing step!")
        
        cell_labels_raw = adata.obs[cluster_key].to_numpy() if cluster_key in adata.obs else np.array(['Unknown' for i in range(adata.n_obs)])
        
        print("*********        Creating Training/Validation Datasets        *********")
        train_set = SCData(X[self.train_idx], cell_labels_raw[self.train_idx], self.decoder.Rscore[self.train_idx]) if self.config['weight_sample'] else SCData(X[self.train_idx], cell_labels_raw[self.train_idx])
        test_set = None
        if(len(self.test_idx)>0):
            test_set = SCData(X[self.test_idx], cell_labels_raw[self.test_idx], self.decoder.Rscore[self.test_idx]) if self.config['weight_sample'] else SCData(X[self.test_idx], cell_labels_raw[self.test_idx])
        data_loader = torch.utils.data.DataLoader(train_set, batch_size=self.config["batch_size"], shuffle=True)
        #Automatically set test iteration if not given
        if(self.config["test_iter"] is None):
            self.config["test_iter"] = len(self.train_idx)//self.config["batch_size"]*2
        print("*********                      Finished.                      *********")
        
        gind, gene_plot = getGeneIndex(adata.var_names, gene_plot)
        
        os.makedirs(figure_path, exist_ok=True)
        
        #define optimizer
        print("*********                 Creating optimizers                 *********")
        param_nn = list(self.encoder.parameters())
        param_ode = [self.decoder.alpha, self.decoder.beta, self.decoder.gamma, self.decoder.ton, self.decoder.toff] 
        if(self.config['train_scaling']):
            param_ode = param_ode+[self.decoder.scaling]
        if(self.config['train_std']):
            param_ode = param_ode+[self.decoder.sigma_u, self.decoder.sigma_s]

        optimizer = torch.optim.Adam(param_nn, lr=self.config["learning_rate"], weight_decay=self.config["lambda"])
        optimizer_ode = torch.optim.Adam(param_ode, lr=self.config["learning_rate_ode"])
        print("*********                      Finished.                      *********")
      
        #Main Training Process
        print("*********                    Start training                   *********")
        print(f"Total Number of Iterations Per Epoch: {len(data_loader)}, test iteration: {self.config['test_iter']}")
        
        n_epochs, n_save = self.config["n_epochs"], self.config["save_epoch"]
        n_warmup = self.config["n_warmup"]
        
        start = time.time()
        for epoch in range(n_epochs):
            #Train the encoder
            if(self.config["k_alt"] is None):
                stop_training = self.train_epoch(data_loader, test_set, optimizer)
                if(epoch>=n_warmup):
                    stop_training_ode = self.train_epoch(data_loader, test_set, optimizer_ode)
                    if(stop_training_ode):
                        print(f"********* Early Stop Triggered at epoch {epoch+1}. *********")
                        break
            else:
                if(epoch>=n_warmup):
                    stop_training = self.train_epoch(data_loader, test_set, optimizer_ode, optimizer, self.config["k_alt"])
                else:
                    stop_training = self.train_epoch(data_loader, test_set, optimizer, None, self.config["k_alt"])
            
            if(plot and (epoch==0 or (epoch+1) % self.config["save_epoch"] == 0)):
                elbo_train = self.test(train_set,
                                       Xembed[self.train_idx],
                                       f"train{epoch+1}", 
                                       False,
                                       gind, 
                                       gene_plot,
                                       True, 
                                       figure_path)
                self.setMode('train')
                elbo_test = self.loss_test[-1] if len(self.loss_test)>0 else -np.inf
                print(f"Epoch {epoch+1}: Train ELBO = {elbo_train:.3f}, Test ELBO = {elbo_test:.3f}, \t Total Time = {convertTime(time.time()-start)}")
                
            
            
            if(stop_training):
                print(f"********* Early Stop Triggered at epoch {epoch+1}. *********")
                break
                
                
        print(f"*********              Finished. Total Time = {convertTime(time.time()-start)}             *********")
        plot_train_loss(self.loss_train, range(1,len(self.loss_train)+1), save=f'{figure_path}/train_loss_vanilla.png')
        if(self.config["test_iter"]>0):
            plot_test_loss(self.loss_test, [i*self.config["test_iter"] for i in range(1,len(self.loss_test)+1)], save=f'{figure_path}/test_loss_vanilla.png')
        return
    
    def predAll(self, data, mode='test', output=["uhat", "shat", "t"], gene_idx=None):
        """
        < Description >
        Generate all predictions.
        
        < Input Arguments >
        1.  data [array (N, 2G)] : 
            Input mRNA count
        
        2.  mode [string]
            train or test or both
            
        3.  output [list of string]
            (Optional) variables to compute
        
        4.  gene_idx [int array/list]
            (Optional) gene index, used for reducing unnecessary memory usage
        
        < Output >
        1.  out [tuple of array]
            Depends on the input argument output.
            Can return predicted u,s and time
        
        2. ELBO [float]
        """
        N, G = data.shape[0], data.shape[1]//2
        if("uhat" in output):
            Uhat = None if gene_idx is None else np.zeros((N,len(gene_idx)))
        if("shat" in output):
            Shat = None if gene_idx is None else np.zeros((N,len(gene_idx)))
        if("t" in output):
            t_out = np.zeros((N))
            std_t_out = np.zeros((N))
        elbo = 0
        with torch.no_grad():
            B = min(N//10, 1000)
            Nb = N // B
            for i in range(Nb):
                data_in = torch.tensor(data[i*B:(i+1)*B]).float().to(self.device)
                mu_tx, std_tx, uhat, shat = self.evalModel(data_in)
                if(mode=="test"):
                    p_t = self.p_t[:,self.test_idx[i*B:(i+1)*B],:]
                elif(mode=="train"):
                    p_t = self.p_t[:,self.train_idx[i*B:(i+1)*B],:]
                else:
                    p_t = self.p_t[:,i*B:(i+1)*B,:]
                loss = self.VAERisk((mu_tx, std_tx), 
                                    p_t,
                                    data_in[:,:G], data_in[:,G:], 
                                    uhat, shat, 
                                    torch.exp(self.decoder.sigma_u), torch.exp(self.decoder.sigma_s), 
                                    None,
                                    1.0)
                elbo = elbo-loss*B
                if("uhat" in output and gene_idx is not None):
                    Uhat[i*B:(i+1)*B] = uhat[:,gene_idx].cpu().numpy()
                if("shat" in output and gene_idx is not None):
                    Shat[i*B:(i+1)*B] = shat[:,gene_idx].cpu().numpy()
                if("t" in output):
                    t_out[i*B:(i+1)*B] = mu_tx.cpu().squeeze().numpy()
                    std_t_out[i*B:(i+1)*B] = std_tx.cpu().squeeze().numpy()
            if(N > B*Nb):
                data_in = torch.tensor(data[B*Nb:]).float().to(self.device)
                mu_tx, std_tx, uhat, shat = self.evalModel(data_in)
                if(mode=="test"):
                    p_t = self.p_t[:,self.test_idx[B*Nb:],:]
                elif(mode=="train"):
                    p_t = self.p_t[:,self.train_idx[B*Nb:],:]
                else:
                    p_t = self.p_t[:,B*Nb:,:]
                loss = self.VAERisk((mu_tx, std_tx), 
                                    p_t,
                                    data_in[:,:G], data_in[:,G:], 
                                    uhat, shat, 
                                    torch.exp(self.decoder.sigma_u), torch.exp(self.decoder.sigma_s), 
                                    None,
                                    1.0)
                elbo = elbo-loss*(N-B*Nb)
                if("uhat" in output and gene_idx is not None):
                    Uhat[Nb*B:] = uhat[:,gene_idx].cpu().numpy()
                if("shat" in output and gene_idx is not None):
                    Shat[Nb*B:] = shat[:,gene_idx].cpu().numpy()
                if("t" in output):
                    t_out[Nb*B:] = mu_tx.cpu().squeeze().numpy()
                    std_t_out[Nb*B:] = std_tx.cpu().squeeze().numpy()
        out = []
        if("uhat" in output):
            out.append(Uhat)
        if("shat" in output):
            out.append(Shat)
        if("t" in output):
            out.append(t_out)
            out.append(std_t_out)
        return out, elbo.cpu().item()/N
    
    def test(self,
             test_set, 
             Xembed,
             testid=0, 
             test_mode=True,
             gind=None, 
             gene_plot=None,
             plot=False, 
             path='figures', 
             **kwargs):
        """
        < Description >
        Evaluate the model upon training/test dataset.
        
        < Input Arguments >
        1.  test_set [torch.utils.data.Dataset]
            Training or validation dataset
        
        2.  Xembed [array]
            Low-dimensional embedding for plotting
        
        3.  testid [string/int]
            (Optional) Used to name the figures.
        
        4.  gind [int array]
            (Optional) Index of genes in adata.var_names.
            Used for plotting.
        
        5.  gene_plot [string array]
            (Optional) Gene names.
        
        6.  plot [bool]
            (Optional) Whether to generate plots.
        
        7.  path [string]
            (Optional) Saving path.
        
        < Output >
        1.  elbo [float]
        """
        self.setMode('eval')
        data = test_set.data
        mode = "test" if test_mode else "train"
        out, elbo = self.predAll(data, mode, gene_idx=gind)
        Uhat, Shat, t, std_t = out[0], out[1], out[2], out[3]
        
        G = data.shape[1]//2
        if(plot):
            ton, toff = np.exp(self.decoder.ton.detach().cpu().numpy()), np.exp(self.decoder.toff.detach().cpu().numpy())
            state = np.ones(toff.shape)*(t.reshape(-1,1)>toff)+np.ones(ton.shape)*2*(t.reshape(-1,1)<ton)
            #Plot Time
            plot_time(t, Xembed, save=f"{path}/time-{testid}-vanilla.png")
            
            #Plot u/s-t and phase portrait for each gene
            for i in range(len(gind)):
                idx = gind[i]
                
                plot_phase(data[:,idx], data[:,idx+G],  
                           Uhat[:,i], Shat[:,i], 
                           gene_plot[i], 
                           None, 
                           state[:,idx], 
                           ['Induction', 'Repression', 'Off'],
                           save=f"{path}/phase-{gene_plot[i]}-{testid}-vanilla.png")
                
                plot_sig(t.squeeze(), 
                         data[:,idx], data[:,idx+G],  
                         Uhat[:,i], Shat[:,i], 
                         test_set.labels,
                         gene_plot[i], 
                         save=f"{path}/sig-{gene_plot[i]}-{testid}-vanilla.png",
                         sparsify=self.config["sparsify"])
        
        return elbo
        
        
    def saveModel(self, file_path, enc_name='encoder_vanilla', dec_name='decoder_vanilla'):
        """
        < Description >
        Save the encoder parameters to a .pt file.
        """
        os.makedirs(file_path, exist_ok=True)
        torch.save(self.encoder.state_dict(), f"{file_path}/{enc_name}.pt")
        torch.save(self.decoder.state_dict(), f"{file_path}/{dec_name}.pt")
        
    def saveAnnData(self, adata, key, file_path, file_name=None):
        """
        < Description >
        Save the ODE parameters and cell time to the anndata object and write it to disk.
        """
        os.makedirs(file_path, exist_ok=True)
        
        self.setMode('eval')
        adata.var[f"{key}_alpha"] = np.exp(self.decoder.alpha.detach().cpu().numpy())
        adata.var[f"{key}_beta"] = np.exp(self.decoder.beta.detach().cpu().numpy())
        adata.var[f"{key}_gamma"] = np.exp(self.decoder.gamma.detach().cpu().numpy())
        adata.var[f"{key}_toff"] = np.exp(self.decoder.toff.detach().cpu().numpy())
        adata.var[f"{key}_ton"] = np.exp(self.decoder.ton.detach().cpu().numpy())
        adata.var[f"{key}_scaling"] = np.exp(self.decoder.scaling.detach().cpu().numpy())
        adata.var[f"{key}_sigma_u"] = np.exp(self.decoder.sigma_u.detach().cpu().numpy())
        adata.var[f"{key}_sigma_s"] = np.exp(self.decoder.sigma_s.detach().cpu().numpy())
        scaling = adata.var[f"{key}_scaling"].to_numpy()
        
        out, elbo = self.predAll(np.concatenate((adata.layers['Mu'], adata.layers['Ms']),axis=1), mode="both", gene_idx=np.array(range(adata.n_vars)))
        Uhat, Shat, t, std_t = out[0], out[1], out[2], out[3]
        
        adata.obs[f"{key}_time"] = t
        adata.obs[f"{key}_std_t"] = std_t
        adata.layers[f"{key}_uhat"] = Uhat
        adata.layers[f"{key}_shat"] = Shat
        
        adata.uns[f"{key}_train_idx"] = self.train_idx
        adata.uns[f"{key}_test_idx"] = self.test_idx
        
        rnaVelocityVanillaVAE(adata, key)
        
        if(file_name is not None):
            adata.write_h5ad(f"{file_path}/{file_name}")
        
        
    
