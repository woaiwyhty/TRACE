"""
Temporal Neighborhood Coding (TNC) for unsupervised learning representation of non-stationary time series
"""

from pandas.core.indexes.base import Index
import torch
from torch.utils import data
import matplotlib.pyplot as plt
import argparse
import math
import seaborn as sns; sns.set()
import sys
# import statsmodels.api as sm # THIS CAUSES TORCH LOAD TO NOT WORK
import numpy as np
import pickle
import os
import random
os.environ['MKL_THREADING_LAYER'] = 'GNU' # Set this value to allow grid_search.py to work.
from sklearn.metrics import silhouette_score, davies_bouldin_score
from sklearn.cluster import AgglomerativeClustering
from datetime import datetime
from tnc.models import CNN_Transformer_Encoder, EncoderMultiSignalMIMIC, GRUDEncoder, RnnEncoder, WFEncoder, TST, EncoderMultiSignal, LinearClassifier, RnnPredictor, EncoderMultiSignalMIMIC, CausalCNNEncoder
from tnc.utils import plot_heatmap, PCA_valid_dataset_kmeans_labels, plot_normal_and_mortality, plot_pca_trajectory
from tnc.evaluations import WFClassificationExperiment, ClassificationPerformanceExperiment
from statsmodels.tsa import stattools
from sklearn.decomposition import PCA
from sklearn.metrics import roc_auc_score, precision_recall_curve, auc, classification_report
import hdbscan

import yaml

if not sys.warnoptions:
    import warnings
    warnings.simplefilter("ignore")
device = 'cuda' if torch.cuda.is_available() else 'cpu'

counter0 = 0
counter1 = 0
nghd_sizes = {}

num_neg_samples_removed = {}

global DEBUG
DEBUG = False
global UNIQUE_ID
UNIQUE_ID = None

######################################################################################################
class Discriminator(torch.nn.Module):
    def __init__(self, input_size, device):
        super(Discriminator, self).__init__()
        self.device = device
        self.input_size = input_size # This is the encoding_size from the encoder. i.e. the dimension of the latent state
                                                # This is 2*self.input_size because we concatenate two encodings
        self.model = torch.nn.Sequential(torch.nn.Linear(2*self.input_size, 4*self.input_size),
                                         torch.nn.ReLU(inplace=True),
                                         torch.nn.Dropout(0.5),
                                         torch.nn.Linear(4*self.input_size, 1))

        torch.nn.init.xavier_uniform_(self.model[0].weight)
        torch.nn.init.xavier_uniform_(self.model[3].weight)

    def forward(self, x, x_tild):
        """
        Predict the probability of the two inputs belonging to the same neighbourhood.
        """
        x_all = torch.cat([x, x_tild], -1)
        p = self.model(x_all)
        return p.view((-1,)) # returns output of the discriminator, its a scaler wrapped in a tensor

######################################################################################################
class TNCDataset(data.Dataset):
    def __init__(self, x, mc_sample_size, window_size, eta=3, state=None, adf=False, acf=False, acf_plus=False, ACF_nghd_Threshold=0.4, ACF_out_nghd_Threshold=0.5):
        super(TNCDataset, self).__init__()
        self.time_series = x # Time series of shape (num_samples, 1, num_features, signal_length) if we have no maps, (num_samples, 2, num_features, signal_length) if we do
        self.T = x.shape[-1] # length of the time series
        self.window_size = window_size
        self.have_map = True if self.time_series.shape[1] == 2 else False
        # self.sliding_gap = int(window_size*25.2) # Commented out because its not used..
        # self.window_per_sample = (self.T-2*self.window_size)//self.sliding_gap # Commented out because its not used..
        self.mc_sample_size = mc_sample_size # num of montecarlo samples for estimating the expectations in the loss
        self.state = state # State of the patients. We have a value for each time step for each patient
        self.adf = adf # Boolean for if we want to do the ADF test
        self.acf = acf # Boolean for if we want to do Autocorrelation for nghd determination
        self.acf_plus = acf_plus # Boolean for if we want to do Autocorrelation for nghd choice AND use it for remove negative samples that are correlated to the nghd
        self.ACF_nghd_Threshold = ACF_nghd_Threshold
        self.ACF_out_nghd_Threshold = ACF_out_nghd_Threshold

        if not self.adf and not self.acf:
            self.eta = eta
            self.nghd_size = 3*window_size*eta
        
        
        self.acf_avgs = [] # Will store a list of acf values for a given sample. Will be modified on each call to _find_neighbors 
        for i in range(len(x)):
            acfs = []
            sample = x[i]
            for f in range(sample.shape[-2]):
                if len(torch.where(sample[1, f, :]==1)[0]) > sample.shape[-2]*0.4: # If more than 40% the data for this feature is observed, compute acf for it. else dont
                    acfs.append(torch.Tensor(stattools.acf(sample[0, f, :], nlags=sample.shape[-1] - 1)))
            acfs = torch.stack(acfs)
            self.acf_avgs.append(torch.mean(acfs, axis=0))
            


    def __len__(self):
        # self.augmentation is used when there are very few samples of data, but they are long. In that case, we may wish to break up the big samples into medium samples
        # e.g. if we have 20 really long samples, we may split them each into 2 samples, meaning we now have 40 samples
        return len(self.time_series) #*self.augmentation

    def __getitem__(self, index):
        '''When a TNCDataset object element is accessed with data[index] notation (but more importantly when you loop through it), it will return some window W_t of the index'th sample timeseries,
        a tensor X_close of self.mc_sample_size windows in the neighborhood and X_distant a tensor of self.mc_sample_size
        windows outside of the neighborhood, as well as y_t which is the approximated patient state
        over the window W_t'''
        end_T = self.T
        start_T = 0 # end_T and start_T represent the start of actual data and the end of actual data. i.e. the time range where each edge of the range does not have missingness (there can obviously be missing values in the middle though)
        if self.have_map: # if we have the map
            # then we can check for places where the data goes to all 0's (i.e. a time point after which all data is missing for any one feature)
            x_map = self.time_series[index][1] # of shape (num_features, signal_length). 0's in the map indicate missingness in the data

            x_map = 1-x_map # Switch to 0's indicating observed, 1's indicating missingness
            result = x_map[0]
            for i in range(1, x_map.shape[0]):
                result = torch.logical_and(result, x_map[i])
            # result is a vector that is 1 for time steps that were totally missing. 0 indicates at least one feature was observed
            x_map = 1-x_map # switch back to 1 indicating observed, 0 meaning missingness
            result = ~result # Now result has 0 to indicate time steps that were completely missing, 1 for steps that had some observed values
            end_offset = 1
            start_offset = 0
            while True:
                if result[x_map.shape[1] - end_offset] == 1.:
                    break 
                
                end_offset += 1

            while True:
                if result[start_offset] == 1.:
                    break
                
                start_offset += 1
            
            # end_offset is now the number of indexes at the end in which all features are missing. e.g. if offset = 5, then for the last 5 time steps all features are missing.
            # start_offset is now the number of indexes at the beinning where all features are missing.
            
            end_T = end_T - end_offset + 1
            start_T += start_offset

        index = index%len(self.time_series) # index for a sample of the full dataset self.time_series
        
        t = np.random.randint(start_T + 2*self.window_size, end_T-2*self.window_size) # randomly select t, the center of the window

        # self.time_series[ind] returns a 2D matrix for the ind'th sample. Shape is (num_features, signal_length)
        W_t = self.time_series[index][:, :, t-self.window_size//2:t+self.window_size//2] # Generate the window, this is W_t from the paper
        # plt.savefig('./plots/%s_seasonal.png'%index) 

        
        X_close = self._find_neighbors(self.time_series[index], t, start_T, end_T, index)
       
        X_distant = self._find_non_neighbors(self.time_series[index], t, start_T, end_T, index)
        

        if self.state is None: # If we have no patient state values
            y_t = -1
        else:
            if len(self.state.shape) == 1:
                y_t = self.state[index]
            elif len(self.state.shape) == 2:
                # self.state is of shape (num_samples, signal_length)
                y_t = torch.round(torch.mean(self.state[index][t-self.window_size//2:t+self.window_size//2]))


        
        # W_t is of shape (num_features, window_size)
        # X_close is of shape (mc_sample_size, num_features, window_size), so its a 'list' of mc_sample_size windows from the nghd
        # X_distant is of shape (mc_sample_size, num_features, window_size), so its a 'list' of mc_sample_size windows from outside the nghd
        # y_t is an integer
        return W_t, X_close, X_distant, y_t

    def _find_neighbors(self, x, t, start_T, end_T, index):
        '''Will find the neighborhood centered at t. x is a tensor for a single sample, shape is (1, num_features, signal_length) if no map, (2, num_features, signal_length) if we have map.
        Note: The T parameter is at most self.T, but can be less if the sample has mising values at the end.'''
        delta = self.window_size
        if self.adf:
            corr = []
            for w_t in range(self.window_size, 4*self.window_size, self.window_size): # Stepping by window_size chunks
                # 4*window_size is the farthest we'll consider away from t for the neighborhood
                
                try:
                    p_val = 0
                    for f in range(x.shape[-2]): # iterating through features
                        # Do ADF test for each feature separately on the window [t-w_t, t+w_t]
                        # x[:, 0, :, :] just isolates the data, leaves out map
                        p = stattools.adfuller(np.array(x[:, 0, :, :][f][max(start_T,t - w_t):min(end_T, t + w_t)].reshape(-1, )))[1]
                        p_val += 0.01 if math.isnan(p) else p
                    
                    corr.append(p_val/x.shape[-2]) # append the average p value over the features to corr
                except: # ??? why try except?
                    corr.append(0.6) # Why add .6?
            # .01 is the p value threshhold
            self.eta = len(corr) if len(np.where(np.array(corr) >= 0.01)[0])==0 else (np.where(np.array(corr) >= 0.01)[0][0] + 1)
            
            # self.nghd_size is 1 standard deviation away from the mean of the nghd distribution. This far away, we will consider samples 'out of the neighborhood'
            self.nghd_size = self.eta*delta

        elif self.acf or self.acf_plus:
            acf_seq = self.acf_avgs[index]

            # Find first index where acf is < ACF_nghd_Threshold. i.e. the lag where autocorrelation is < ACF_nghd_Threshold. If there isn't, set it to the len of acfs
            avg_index = np.where(acf_seq < self.ACF_nghd_Threshold)[0][0] if len(np.where(acf_seq < self.ACF_nghd_Threshold)[0]) > 0 else len(acf_seq)

            self.eta = 0
            while self.eta*self.window_size <= avg_index and self.eta < 10:
                self.eta += 1
            self.nghd_size = self.eta*delta
        
        if self.nghd_size in nghd_sizes:
            nghd_sizes[self.nghd_size] += 1
        else:
            nghd_sizes[self.nghd_size] = 0
            
        ## Random from a Gaussian
        # t_p is a tensor of time values that will act as the centers of windows *in* the nbhd. There are self.mc_sample_size of them
        t_p = [int(t+np.random.randn()*self.nghd_size) for _ in range(self.mc_sample_size)]
        # Selecting time values that will allow windows to fit
        t_p = [max(start_T + self.window_size//2+1,min(t_pp, end_T-self.window_size//2)) for t_pp in t_p]
        
        # Stacking together windows in the same nghd
        x_p = torch.stack([x[:, :, t_ind-self.window_size//2:t_ind+self.window_size//2] for t_ind in t_p])
        return x_p

    def _find_non_neighbors(self, x, t, start_T, end_T, index):
        '''Will find non neighbors of the neighbordhood centered at t. x is a tensor for a single sample, shape is (1, num_features, signal_length) if we have no map, (2, num_features, signal_length) if we do
        Note: The T parameter is at most self.T, but can be less if the sample has mising values at the end.'''
        if self.acf_plus:
            # Recall self.nghd_size is the size of 1 standard deviation of the normal distribution that defines our neighborhood
            if t-start_T < 2*self.nghd_size: # if t is so close to the start that the nghd starts at start_T, then only select samples from the right
                t_n = np.random.randint(t + self.nghd_size + self.window_size//2, end_T - self.window_size//2, self.mc_sample_size)

            elif end_T - t < 2*self.nghd_size: # if t is so close to the end that the nghd ends at end_T, then only select samples from the left
                t_n = np.random.randint(start_T + self.window_size//2 + 1, end_T - self.nghd_size - self.window_size//2, self.mc_sample_size)


            else: # In the case where t falls somewhere in between, select negative samples proportionally on each side of the neighborhood
                start_of_nghd = t - self.nghd_size
                end_of_nghd = t + self.nghd_size

                proportion_to_right = (end_T - end_of_nghd)/((end_T - end_of_nghd) + (start_of_nghd - start_T))

                mc_sample_right = int(self.mc_sample_size*proportion_to_right)
                mc_sample_left = self.mc_sample_size-mc_sample_right

                t_left = np.random.randint(start_T + self.window_size//2 + 1, start_of_nghd, mc_sample_left)
                t_right = np.random.randint(end_of_nghd + self.window_size//2, end_T - self.window_size//2, mc_sample_right)

                t_n = np.concatenate([t_left, t_right])

            '''
            if t>(end_T-start_T)/2:
                # if t is in the second half of the time series, take non neighbors from the first half
                t_n = np.random.randint(start_T + self.window_size//2, max((t - self.nghd_size + 1), start_T + self.window_size//2+1), self.mc_sample_size)
            else:
                # if t is in the first half of the time series, take non neighbors from the second half
                t_n = np.random.randint(min((t + self.nghd_size), (end_T - self.window_size-1)), (end_T - self.window_size//2), self.mc_sample_size)
            '''

            t_n_final = []
            avg_acfs = self.acf_avgs[index]
            neg_sample_lags = np.array([abs(t-t_prime) for t_prime in t_n])
            for t_prime in t_n:
                include_t_n = True
                
                acf_avgs = self.acf_avgs[index]
                if abs(acf_avgs[abs(t-t_prime)]) > self.ACF_out_nghd_Threshold:
                    include_t_n = False
                
                if include_t_n:
                    t_n_final.append(t_prime)
            t_n_final = np.array(t_n_final)
            
            # Logging how many samples we are removing from the original negatives chosen
            diff = (len(t_n) - len(t_n_final))
            if diff in num_neg_samples_removed:
                num_neg_samples_removed[diff] += 1
            else:
                num_neg_samples_removed[diff] = 0

            
            # if we cut down some negative samples, we'll repeat the existing ones so we have mc_sample_size of them
            if len(t_n_final > 0):
                t_n = t_n_final
                while len(t_n) < self.mc_sample_size:
                    t_n = np.concatenate([t_n, t_n])
                t_n = t_n[0:self.mc_sample_size]
            else:
                # if we have removed all negative samples, grab the ones that had the lowest average correlation across features
                best_lags_inds = np.argsort(avg_acfs[neg_sample_lags])[:self.mc_sample_size]
                best_neg_sample_lags = neg_sample_lags[best_lags_inds]
                t_n_final = []
                for t_prime in t_n:
                    if abs(t-t_prime) in best_neg_sample_lags:
                        t_n_final.append(t_prime)
                
                assert len(t_n_final) != 0
                t_n = t_n_final
                while len(t_n) < self.mc_sample_size:
                    print("While loop in TNCDataset entered..")
                    t_n = np.concatenate([t_n, t_n])
                t_n = t_n[0:self.mc_sample_size]

        else:
            if t>(end_T-start_T)/2:
                # if t is in the second half of the time series, take non neighbors from the first half
                t_n = np.random.randint(start_T + self.window_size//2, max((t - self.nghd_size + 1), start_T + self.window_size//2+1), self.mc_sample_size)
            else:
                # if t is in the first half of the time series, take non neighbors from the second half
                t_n = np.random.randint(min((t + self.nghd_size), (end_T - self.window_size-1)), (end_T - self.window_size//2), self.mc_sample_size)
       
        if len(t_n) > 0:
            x_n = torch.stack([x[:, :, t_ind-self.window_size//2:t_ind+self.window_size//2] for t_ind in t_n])
        else:
            rand_t = np.random.randint(0,self.window_size//5)
            if t > (end_T-start_T) / 2:
                x_n = x[:, :,start_T + rand_t:start_T + rand_t+start_T+self.window_size].unsqueeze(0)
            else:
                x_n = x[:, :, end_T - rand_t - self.window_size:end_T - rand_t].unsqueeze(0)
        return x_n

######################################################################################################

def linear_classifier_epoch_run(data, labels, train, num_pre_positive_encodings, batch_size, rnn, classifier, optimizer, encoder):
    epoch_losses = []
    epoch_predictions = []
    epoch_labels = []
    for i in range(data.shape[0]//batch_size): # Split into batches for training
        if i == data.shape[0]//batch_size - 1:
            data_batch = data[i*batch_size:]
            label_batch = labels[i*batch_size:].to(device)    
        else:
            data_batch = data[i*batch_size: (i+1)*batch_size] # of shape (batch_size, 2, num_features, seq_len)
            label_batch = labels[i*batch_size: (i+1)*batch_size].to(device) # of shape (batch_size,)
        
        
        for j, sample in enumerate(data_batch):
            if sample[1, 0, 0] == -1:
                break
        

        # encoding_batch is of shape (batch_size, seq_len/window_size, encoding_size)
        encoding_batch, encoding_mask = encoder.forward_seq(data_batch, return_encoding_mask=True) # encoding_mask is of shape (batch_size, seq_len/window_size)
        encoding_batch = encoding_batch.to(device)
        encoding_mask = encoding_mask.to(device)
        

        # encoding_batch and label_batch are of size (batch_size, seq_len/window_size, encoding_size) and (batch_size)
        rnn_window_size = int(torch.randint(low=1, high=4, size=(1,))) # generates a number between 1 and 3 inclusive. This is the number of encodings the rnn will be fed at a time
        positive_inds = torch.where(label_batch==1)
        positive_encodings = encoding_batch[positive_inds] # of shape (num_pos_in_batch, seq_len/window_size, encoding_size)
        positive_encodings_mask = encoding_mask[positive_inds] # of shape (num_pos_in_batch, seq_len/window_size)
        
        negative_inds = torch.where(label_batch==0)
        negative_encodings = encoding_batch[negative_inds] # of shape (num_neg_in_batch, seq_len/window_size, encoding_size)
        negative_encodings_mask = encoding_mask[negative_inds] # of shape (num_neg_in_batch, seq_len/window_size)

        
        positive_encodings = positive_encodings[:, -num_pre_positive_encodings:, :] # now of shape (num_pos_in_batch, num_pre_positive_encodings, encoding_size)
        positive_encodings_mask = positive_encodings_mask[:, -num_pre_positive_encodings:] # now of shape (num_pos_in_batch, num_pre_positive_encodings)
        

        positive_encodings = positive_encodings[:, -(positive_encodings.shape[1]//rnn_window_size)*rnn_window_size:, :] # Clips each sample on the left side so the number of encodings is divisible by rnn_window_size
        positive_encodings_mask = positive_encodings_mask[:, -(positive_encodings_mask.shape[1]//rnn_window_size)*rnn_window_size:]
        negative_encodings = negative_encodings[:, -(negative_encodings.shape[1]//rnn_window_size)*rnn_window_size:, :]
        negative_encodings_mask = negative_encodings_mask[:, -(negative_encodings_mask.shape[1]//rnn_window_size)*rnn_window_size:]


        negative_encodings = negative_encodings.reshape(-1, rnn_window_size, negative_encodings.shape[-1]) # Now of shape (num_neg_rnn_window_sizes_over_all_encodings, rnn_window_size, encoding_size)
        positive_encodings = positive_encodings.reshape(-1, rnn_window_size, positive_encodings.shape[-1]) # Now of shape (num_pos_rnn_window_sizes_over_all_encodings, rnn_window_size, encoding_size)
        negative_encodings_mask = negative_encodings_mask.reshape(-1, rnn_window_size) # Now of shape (num_neg_rnn_window_sizes_over_all_encodings, rnn_window_size)
        positive_encodings_mask = positive_encodings_mask.reshape(-1, rnn_window_size) # Now of shape (num_pos_rnn_window_sizes_over_all_encodings, rnn_window_size)

        # Now we'll do the mode of each rnn window of encodings. So for each sequence of encodings, we'll take the mode value of the mask
        negative_encodings_mask = torch.mode(negative_encodings_mask, dim=1)[0] # of shape (num_neg_rnn_window_sizes_over_all_encodings,)
        positive_encodings_mask = torch.mode(positive_encodings_mask, dim=1)[0] # of shape (num_pos_rnn_window_sizes_over_all_encodings,)

        neg_window_labels = torch.zeros(negative_encodings.shape[0], 1)
        pos_window_labels = torch.ones(positive_encodings.shape[0], 1)
        

        window_samples = torch.vstack([positive_encodings, negative_encodings]) # of shape (num_rnn_window_sizes_over_all_encodings, rnn_window_size, encoding_size)
        window_labels = torch.cat([pos_window_labels, neg_window_labels]) # of shape (num_rnn_window_sizes_over_all_encodings, 1)
        window_masks = torch.cat([positive_encodings_mask, negative_encodings_mask]) # of shape (num_rnn_window_sizes_over_all_encodings,)

        # Remove all sequences of encodings that were at least partly derived from fully imputed data.
        window_samples = window_samples[torch.where(window_masks!=-1)]
        window_labels = window_labels[torch.where(window_masks!=-1)]


        '''
        print("SHUFFLING DATA BEFORE FEEDING INTO RNN!")
        # Shuffling before feeding to RNN
        inds = np.arange(len(window_samples))
        np.random.shuffle(inds)
        window_samples = window_samples[inds]
        window_labels = window_labels[inds]
        window_masks = window_masks[inds]
        '''

        _, hidden_and_cell = rnn(window_samples)
        hidden_state = hidden_and_cell[0] # hidden state is of shape (1, batch_size, hidden_size). Contains the last hidden state for each sample in the batch
        hidden_state = torch.squeeze(hidden_state)
        
        predictions = torch.squeeze(classifier(hidden_state))
        # Shape of predictions and window_labels is (batch_size)
        window_labels = torch.squeeze(window_labels).to(device)
        if train:
            # Ratio of num negative examples divided by num positive examples is pos_weight
            pos_weight = torch.Tensor([negative_encodings.shape[0] / max(positive_encodings.shape[0], 1)]).to(device)
            loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight) # Applies sigmoid to outputs passed in so we shouldn't have sigmoid in the model. 
            loss = loss_fn(predictions, window_labels)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            

        else:
            loss_fn = torch.nn.BCEWithLogitsLoss()
            loss = loss_fn(predictions, window_labels)
        
        epoch_loss = loss.item()

        # Apply sigmoid to predictions since we didn't apply it for the loss function since the loss function does sigmoid on its own.
        predictions = torch.nn.Sigmoid()(predictions)

        # Move Tensors to CPU and remove gradients so they can be converted to NumPy arrays in the sklearn functions
        window_labels = window_labels.cpu().detach()
        predictions = predictions.cpu().detach()
        encoding_batch = encoding_batch.cpu() # Move off GPU memory
        neg_window_labels = neg_window_labels.cpu()
        pos_window_labels = pos_window_labels.cpu()
        label_batch = label_batch.cpu()

        epoch_losses.append(epoch_loss)
        epoch_predictions.append(predictions)
        epoch_labels.append(window_labels)
    
    return epoch_predictions, epoch_losses, epoch_labels

def train_linear_classifier(X_train, y_train, X_validation, y_validation, X_TEST, y_TEST, encoding_size, num_pre_positive_encodings, encoder, batch_size=32, return_models=False, return_scores=False, pos_sample_name='arrest', data_type='ICU', classification_cv=0, encoder_cv=0):
    '''
    Trains an RNN and linear classifier jointly. X_train is of shape (num_samples, num_windows_per_hour, encoding_size)
    and y_train is of shape (num_samples)

    '''

    print("Training Linear Classifier", flush=True)
    rnn = RnnPredictor(encoding_size=encoding_size, hidden_size=32).to(device)
    # 32 is the arbitrary hidden size chosen for the RNN
    classifier = LinearClassifier(input_size=32).to(device)
    

    if len(tuple(y_train.shape)) == 2: # If labels are in 2D array
        y_train = torch.squeeze(y_train)
        y_validation = torch.squeeze(y_validation) # Make them 1D (e.g. 32 instead of 32x1)
    
    
    params = list(classifier.parameters()) + list(rnn.parameters())
    optimizer = torch.optim.Adam(params, lr=.001, weight_decay=.005)
    
    for epoch in range(1, 101):
        classifier.train()
        rnn.train()
        encoder.eval()

        epoch_train_predictions, epoch_train_losses, epoch_train_labels = linear_classifier_epoch_run(data=X_train, labels=y_train, train=True, 
                                                    num_pre_positive_encodings=num_pre_positive_encodings,
                                                    batch_size=batch_size, rnn=rnn, classifier=classifier,
                                                    optimizer=optimizer, encoder=encoder)

        
        classifier.eval()
        rnn.eval()
        epoch_validation_predictions, epoch_validation_losses, epoch_validation_labels = linear_classifier_epoch_run(data=X_validation, labels=y_validation, train=False, 
                                                    num_pre_positive_encodings=num_pre_positive_encodings,
                                                    batch_size=batch_size, rnn=rnn, classifier=classifier,
                                                    optimizer=optimizer, encoder=encoder)

        
        

        epoch_TEST_predictions, epoch_TEST_losses, epoch_TEST_labels = linear_classifier_epoch_run(data=X_TEST, labels=y_TEST, train=False, 
                                                    num_pre_positive_encodings=num_pre_positive_encodings,
                                                    batch_size=batch_size, rnn=rnn, classifier=classifier,
                                                    optimizer=optimizer, encoder=encoder)

        
        # TRAIN 
        # Compute average over all batches in the epoch
        epoch_train_loss = np.mean(epoch_train_losses)
        epoch_train_predictions = torch.cat(epoch_train_predictions)
        epoch_train_labels = torch.cat(epoch_train_labels)
        print('epoch_train_predictions shape: ', epoch_train_predictions.shape)
        print('epoch_train_labels shape: ', epoch_train_labels.shape)

        epoch_train_auroc = roc_auc_score(epoch_train_labels, epoch_train_predictions)
        # Compute precision recall curve
        precision, recall, _ = precision_recall_curve(epoch_train_labels, epoch_train_predictions)
        # Compute AUPRC
        epoch_train_auprc = auc(recall, precision) # precision is the y axis, recall is the x axis, computes AUC of this curve


        # VALIDATION
        epoch_validation_loss = np.mean(epoch_validation_losses)
        epoch_validation_predictions = torch.cat(epoch_validation_predictions)
        epoch_validation_labels = torch.cat(epoch_validation_labels)

        epoch_validation_auroc = roc_auc_score(epoch_validation_labels, epoch_validation_predictions)
        # Compute precision recall curve
        precision, recall, _ = precision_recall_curve(epoch_validation_labels, epoch_validation_predictions)
        # Compute AUPRC
        epoch_validation_auprc = auc(recall, precision) # precision is the y axis, recall is the x axis, computes AUC of this curve


        # TEST
        epoch_TEST_loss = np.mean(epoch_TEST_losses)
        epoch_TEST_predictions = torch.cat(epoch_TEST_predictions)
        epoch_TEST_labels = torch.cat(epoch_TEST_labels)

        epoch_TEST_auroc = roc_auc_score(epoch_TEST_labels, epoch_TEST_predictions)
        # Compute precision recall curve
        precision, recall, _ = precision_recall_curve(epoch_TEST_labels, epoch_TEST_predictions)
        # Compute AUPRC
        epoch_TEST_auprc = auc(recall, precision) # precision is the y axis, recall is the x axis, computes AUC of this curve
        

        if epoch%10==0:
            print('Epoch %d Classifier Loss =====> Training Loss: %.5f \t Training AUROC: %.5f \t Training AUPRC: %.5f\t Validation Loss: %.5f \t Validation AUROC: %.5f \t Validation AUPRC %.5f\t TEST Loss: %.5f \t TEST AUROC: %.5f \t TEST AUPRC %.5f'
                                % (epoch, epoch_train_loss, epoch_train_auroc, epoch_train_auprc, epoch_validation_loss, epoch_validation_auroc, epoch_validation_auprc, epoch_TEST_loss, epoch_TEST_auroc, epoch_TEST_auprc))
            epoch_train_predictions[epoch_train_predictions >= 0.5] = 1
            epoch_train_predictions[epoch_train_predictions < 0.5] = 0

            epoch_validation_predictions[epoch_validation_predictions >= 0.5] = 1
            epoch_validation_predictions[epoch_validation_predictions < 0.5] = 0

            epoch_TEST_predictions[epoch_TEST_predictions >= 0.5] = 1
            epoch_TEST_predictions[epoch_TEST_predictions < 0.5] = 0
            
            print("Train classification report: ")
            print('epoch_train_labels shape: ', epoch_train_labels.shape, 'epoch_train_predictions shape: ', epoch_train_predictions.shape)
            print(classification_report(epoch_train_labels.to('cpu'), epoch_train_predictions, target_names=['normal', pos_sample_name]))
            print("Validation classification report: ")
            print(classification_report(epoch_validation_labels.to('cpu'), epoch_validation_predictions, target_names=['normal', pos_sample_name]))
            print()
            print("TEST classification report: ")
            print(classification_report(epoch_TEST_labels.to('cpu'), epoch_TEST_predictions, target_names=['normal', pos_sample_name]))
            print()


            # Checkpointing the classifier model

            state = {
                    'epoch': epoch,
                    'rnn_state_dict': rnn.state_dict(),
                    'classifier_state_dict': classifier.state_dict(),
                }

            torch.save(state, '../ckpt/%s/%s_encoder_checkpoint_%d_Classifier_checkpoint_%d.tar'%(data_type, UNIQUE_ID, encoder_cv, classification_cv))
    

    if return_models and return_scores:
        return (rnn, classifier, epoch_validation_auroc, epoch_validation_auprc, epoch_TEST_auroc, epoch_TEST_auprc)
    if return_models:
        return (rnn, classifier)
    if return_scores:
        return (epoch_validation_auroc, epoch_validation_auroc)

    

def get_encoder(encoder_type, encoder_hyper_params):
    '''Takes in a string (e.g. 'Transformer'), and a dictionary of hyperparameters,
    and instantiates the appropriate encoder and returns it.'''

    if encoder_type == 'RNN':
        return RnnEncoder(**encoder_hyper_params)
    elif encoder_type == 'Transformer':
        return TST(**encoder_hyper_params)
    elif encoder_type == 'WF':
        return WFEncoder(**encoder_hyper_params)
    elif encoder_type == 'CNN_RNN':
        if 'MIMIC' in UNIQUE_NAME:
            return EncoderMultiSignalMIMIC(**encoder_hyper_params)
        else:
            return EncoderMultiSignal(**encoder_hyper_params)
    elif encoder_type == 'GRUD':
        return GRUDEncoder(**encoder_hyper_params)
    elif encoder_type == 'CNN_Transformer':
        return CNN_Transformer_Encoder(**encoder_hyper_params)
    elif encoder_type == 'CausalCNNEncoder':
        return CausalCNNEncoder(**encoder_hyper_params)




def epoch_run(loader, disc_model, encoder, device, w=0, optimizer=None, train=True, remove_w=False):
    if train: # Puts encoder and discriminator into train mode
        encoder.train()
        disc_model.train()
    else:
        encoder.eval()
        disc_model.eval()
    # loader is a dataloader containing train or validation data (used as test data here, not used for hyperparamater tuning)
    loss_fn = torch.nn.BCEWithLogitsLoss() # sigmoid followed by binary cross entropy
    encoder.to(device)
    disc_model.to(device)
    
    epoch_loss = 0
    epoch_acc = 0
    batch_count = 0
    for x_t, x_p, x_n, _ in loader:
        # x_t is of shape (batch_size, m, num_features, window_size), where m=1 if we have no maps, m=2 if we do. It is a window of data
        # x_p is of shape (batch_size, mc_sample_size, m, num_features, window_size) (where m=1 if we have no maps, m=2 if we do), so its a 'list' 
        # that is batch_size long (one for each sample in the batch), containing mc_sample_size windows from inside
        # the neighborhood
        # x_n is of shape (batch_size, mc_sample_size, m, num_features, window_size) (where m=1 if we have no maps, m=2 if we do), so its a 'list' 
        # that is batch_size long (one for each sample in the batch), containing mc_sample_size windows from outside
        # the neighborhood
        # _ is a list of integers representing the avg patient state for each of the batch_size windows

        mc_sample_size = x_p.shape[1]
        batch_size, m, num_features, window_size = x_t.shape

        x_p = x_p.reshape((-1, m, num_features, window_size))
        x_n = x_n.reshape((-1, m, num_features, window_size))
        # x_p and x_n are now of shape (batch_size * mc_sample_size, m, num_features, window_size) instead of 
        # (batch_size, mc_sample_size, m, num_features, window_size)

        x_t = torch.repeat_interleave(x_t, mc_sample_size, dim=0)
        # x_t is now of shape (batch_size * mc_sample_size, m, num_features, window_size). The batch_size windows
        # have been repeated mc_sample_size times, so we can do element wise comparision between x_t, x_p, and x_n,
        # and have a direct comparison between a window, a positive sample, and a (potentially) negative sample

        neighbors = torch.ones((len(x_p))).to(device)
        non_neighbors = torch.zeros((len(x_n))).to(device)
        x_t, x_p, x_n = x_t.to(device), x_p.to(device), x_n.to(device)
        
        z_t = encoder(x_t)
        z_p = encoder(x_p)
        z_n = encoder(x_n) # z_t, z_p, and z_n are now all of size (batch_size*mc_sample_size, encoding_size)
        

        
        d_p = disc_model(z_t, z_p)
        d_n = disc_model(z_t, z_n)
        
        p_loss = loss_fn(d_p, neighbors)
        n_loss = loss_fn(d_n, non_neighbors)
        n_loss_u = loss_fn(d_n, neighbors)
        if not remove_w:
            loss = (p_loss + w*n_loss_u + (1-w)*n_loss)/2
        else: #remove_w is True
            loss = (p_loss + n_loss)/2
        
        
        if train:
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        
        p_acc = torch.sum(torch.nn.Sigmoid()(d_p) > 0.5).item() / len(z_p)
        n_acc = torch.sum(torch.nn.Sigmoid()(d_n) < 0.5).item() / len(z_n)
        epoch_acc = epoch_acc + (p_acc+n_acc)/2
        epoch_loss += loss.item()
        batch_count += 1
        
        x_t, x_p, x_n = x_t.to('cpu'), x_p.to('cpu'), x_n.to('cpu') # Move back to cpu
    return epoch_loss/batch_count, epoch_acc/batch_count


def learn_encoder(data_maps, labels, encoder_type, encoder_hyper_params, pretrain_hyper_params, window_size, w, batch_size, lr=0.001, decay=0.005, mc_sample_size=20,
                  n_epochs=100, data_type='simulation', device='cpu', n_cross_val_encoder=1, cont=False, ETA=None, ADF=True, ACF=False, ACF_PLUS=False, ACF_nghd_Threshold=0.4, ACF_out_nghd_Threshold=0.4):
    
    # x is of shape (num_samples, num_features, signal_length) OR (num_samples, 2, num_features, signal_length) if we have maps for
    # our data which indicate where we have missing values. for each sample (which is of shape (2, num_features, signal_length)), sample[0] would be the data, and sample[1] is the map
    
    accuracies, losses = [], []
    for cv in range(n_cross_val_encoder):
        random.seed(21*cv)
        print("LEARN ENCODER CV: ", cv)
        encoder = get_encoder(encoder_type=encoder_type, encoder_hyper_params=encoder_hyper_params)
        encoder = encoder.to(device)
        disc_model = Discriminator(encoder.encoding_size, device)
        params = list(disc_model.parameters()) + list(encoder.parameters())
        optimizer = torch.optim.Adam(params, lr=lr, weight_decay=decay)

        performance = []
        best_acc = 0
        best_loss = np.inf
        if not os.path.exists('../ckpt/%s'%data_type):
            os.mkdir('../ckpt/%s'%data_type)
        epoch_start = 0
        if cont:
            if os.path.exists('../ckpt/%s/%s_checkpoint_%d.tar'%(data_type, UNIQUE_NAME, cv)): # i.e. a checkpoint *has* been saved for this config/cv
                print('Restarting from checkpoint')
                checkpoint = torch.load('../ckpt/%s/%s_checkpoint_%d.tar'%(data_type, UNIQUE_NAME, cv))
                encoder.load_state_dict(checkpoint['encoder_state_dict'])
                disc_model = disc_model.load_state_dict(checkpoint['discriminator_state_dict'])
                epoch_start = checkpoint['epoch'] + 1 # starting point for epochs is whatever was saved last + 1 (i.e. if we finished epoch 10 before saving, want to start on 11)
                performance = checkpoint['performance']
        
        inds = np.arange(len(data_maps))
        np.random.shuffle(inds)
        data_maps = data_maps[inds]
        labels = labels[inds]

        train_data = data_maps[0:int(0.8*len(data_maps))]
        train_labels = labels[0:int(0.8*len(labels))]
        validation_data = data_maps[int(0.8*len(data_maps)):]
        validation_labels = labels[int(0.8*len(labels)):]

        if len(tuple(train_data.shape)) == 3: # if train_data is of shape (num_samples, num_features, signal_length)
            # reshape to (num_samples, 1, num_features, signal_length)
            train_data = torch.reshape(train_data, (train_data.shape[0], 1, train_data.shape[1], train_data.shape[2]))
            validation_data = torch.reshape(validation_data, (validation_data.shape[0], 1, validation_data.shape[1], validation_data.shape[2]))
        
        print("ETA, ADF, ACF, ACF_PLUS: ", ETA, ADF, ACF, ACF_PLUS)
        if not ADF and not ACF and not ACF_PLUS:
            print("NOTE: ADF AND ACF ARE TURNED OFF FOR TNC")
            print("ETA IS MANUALLY SET TO ", ETA)
        elif ACF and not ADF:
            print("USING AUTOCORRELATION")
        elif not ACF and ADF:
            print("USING ADF")
        elif ACF_PLUS:
            print("USING ACF_PLUS")
            
        trainset = TNCDataset(x=train_data, mc_sample_size=mc_sample_size,
                                window_size=window_size, eta=ETA, state=train_labels, adf=ADF, acf=ACF, acf_plus=ACF_PLUS, ACF_nghd_Threshold=ACF_nghd_Threshold, ACF_out_nghd_Threshold=ACF_out_nghd_Threshold)
        
        print('Done with TNCDataset for train data. Moving on to validation data...')
        validset = TNCDataset(x=validation_data, mc_sample_size=mc_sample_size,
                                window_size=window_size, eta=ETA, state=validation_labels, adf=ADF, acf=ACF, acf_plus=ACF_PLUS, ACF_nghd_Threshold=ACF_nghd_Threshold, ACF_out_nghd_Threshold=ACF_out_nghd_Threshold)

        print("Done making TNCDataset object for validation data")

        train_loader = data.DataLoader(trainset, batch_size=batch_size, shuffle=True)
        valid_loader = data.DataLoader(validset, batch_size=batch_size, shuffle=True)

        
        if epoch_start < n_epochs-1:
            for epoch in range(epoch_start, n_epochs+1):
                epoch_loss, epoch_acc = epoch_run(train_loader, disc_model, encoder, optimizer=optimizer,
                                                w=w, train=True, device=device, remove_w=ACF_PLUS)
                validation_loss, validation_acc = epoch_run(valid_loader, disc_model, encoder, train=False, w=w, device=device, remove_w=ACF_PLUS)
                
                performance.append((epoch_loss, validation_loss, epoch_acc, validation_acc))
                if epoch%10 == 0:
                    print('(cv:%s)Epoch %d Encoder Loss =====> Training Loss: %.5f \t Training Accuracy: %.5f \t Validation Loss: %.5f \t Validation Accuracy: %.5f'
                        % (cv, epoch, epoch_loss, epoch_acc, validation_loss, validation_acc))
                    
                    state = {
                        'epoch': epoch,
                        'encoder_state_dict': encoder.state_dict(),
                        'discriminator_state_dict': disc_model.state_dict(),
                        'best_accuracy': validation_acc,
                        'performance': performance,
                        'encoder_hyper_params': encoder_hyper_params,
                        'learn_encoder_hyper_params': LEARN_ENCODER_HYPER_PARAMS,
                        'classification_hyper_params': CLASSIFICATION_HYPER_PARAMS,
                        'pretrain_hyper_params': PRETRAIN_HYPER_PARAMS,
                        'unique_id': UNIQUE_ID,
                        'unique_name': UNIQUE_NAME,
                        'data_type': DATA_TYPE,
                        'encoder_type': ENCODER_TYPE
                    }

                    torch.save(state, '../ckpt/%s/%s_checkpoint_%d.tar'%(data_type, UNIQUE_NAME, cv))

                if best_loss > validation_loss:
                    best_acc = validation_acc
                    best_loss = validation_loss
                    
            accuracies.append(best_acc)
            losses.append(best_loss)
            # Save performance plots
            if not os.path.exists('../DONTCOMMITplots/%s/%s'%(data_type, UNIQUE_ID)):
                os.mkdir('../DONTCOMMITplots/%s/%s'%(data_type, UNIQUE_ID))

            train_loss = [t[0] for t in performance]
            validation_loss = [t[1] for t in performance]
            train_acc = [t[2] for t in performance]
            validation_acc = [t[3] for t in performance]
            
            plt.figure()
            plt.plot(np.arange(n_epochs+1), train_loss, label="Train")
            plt.plot(np.arange(n_epochs+1), validation_loss, label="Validation")
            plt.title("Loss")
            plt.legend()
            plt.savefig(os.path.join("../DONTCOMMITplots/%s/%s"%(data_type, UNIQUE_ID), "%s_loss_%d.pdf"%(UNIQUE_NAME, cv)))
            plt.figure()
            plt.plot(np.arange(n_epochs+1), train_acc, label="Train")
            plt.plot(np.arange(n_epochs+1), validation_acc, label="Validation")
            plt.title("Accuracy")
            plt.legend()
            plt.savefig("../DONTCOMMITplots/%s/%s/%s_discriminator_accuracy_%d.pdf"%(data_type, UNIQUE_ID, UNIQUE_NAME, cv))



            
    print("nghd sizes:", nghd_sizes, flush=True)
    print("Recall, each nghd_size is the size of the standard deviation of the normal distribution defining the nghd")
    print("num_neg_samples_removed: ", num_neg_samples_removed)
    
    print('=======> Performance Summary:')
    print('Accuracy: %.2f +- %.2f'%(100*np.mean(accuracies), 100*np.std(accuracies)))
    print('Loss: %.4f +- %.4f'%(np.mean(losses), np.std(losses)))
    return encoder


def main(train_encoder, data_type, encoder_type, encoder_hyper_params, learn_encoder_hyper_params, classification_hyper_params, cont, pretrain_hyper_params, plot_embeddings, unique_id, unique_name, DEBUG=False):
    torch.cuda.empty_cache()
    global UNIQUE_NAME
    UNIQUE_NAME=unique_name
    global UNIQUE_ID
    UNIQUE_ID=unique_id
    global DATA_TYPE
    DATA_TYPE = data_type
    global ENCODER_TYPE
    ENCODER_TYPE = encoder_type
    
    
    if not os.path.exists("../DONTCOMMITplots/"):
        os.mkdir("../DONTCOMMITplots/")
    if not os.path.exists("../ckpt/"):
        os.mkdir("../ckpt/")
    
    global ECODER_HYPER_PARAMS
    ECODER_HYPER_PARAMS = encoder_hyper_params
    global LEARN_ENCODER_HYPER_PARAMS
    LEARN_ENCODER_HYPER_PARAMS = learn_encoder_hyper_params
    global CLASSIFICATION_HYPER_PARAMS
    CLASSIFICATION_HYPER_PARAMS = classification_hyper_params
    global PRETRAIN_HYPER_PARAMS
    PRETRAIN_HYPER_PARAMS = pretrain_hyper_params

    
    if data_type == 'ICU':
        window_size = learn_encoder_hyper_params['window_size']
        length_of_hour = int(60*60/5)
        pos_sample_name = 'arrest'
        path = '/datasets/sickkids/TNC_ICU_data/'
        signal_list = ["Pulse", "HR", "SpO2", "etCO2", "NBPm", "NBPd", "NBPs", "RR", "CVPm", "awRR"]
        sliding_gap = 20
        pre_positive_window = int(2*(60*60/5))
        num_pre_positive_encodings = int(pre_positive_window/window_size)

        if DEBUG: # Smaller version of dataset to debug code with. Not representitive of the whole cohort, and test data is same as train/valid
            TEST_mixed_data_maps = torch.from_numpy(np.load(os.path.join(path, 'debug_ca_data_maps.npy')))
            TEST_mixed_labels = torch.from_numpy(np.load(os.path.join(path, 'debug_ca_labels.npy')))
            TEST_mixed_data_maps = torch.cat([TEST_mixed_data_maps, TEST_mixed_data_maps])
            TEST_mixed_labels = torch.cat([TEST_mixed_labels, TEST_mixed_labels])

            # Concatenating data to artificially increase dataset size. This is for debugging *code*.
            train_mixed_data_maps = torch.from_numpy(np.load(os.path.join(path, 'debug_ca_data_maps.npy')))
            train_mixed_labels = torch.from_numpy(np.load(os.path.join(path, 'debug_ca_labels.npy')))
            train_mixed_data_maps = torch.cat([train_mixed_data_maps, train_mixed_data_maps])
            train_mixed_labels = torch.cat([train_mixed_labels, train_mixed_labels])

            # set encoder cv and classifier cv to 1 so things run faster
            learn_encoder_hyper_params['n_cross_val_encoder'] = 1
            classification_hyper_params['n_cross_val_classification'] = 1

        else:
            # NOTE THE MAP CHANNEL HAS 1'S FOR OBSERVED VALUES, 0'S FOR MISSING VALUES
            # data_maps arrays are of shape (num_samples, 2, 10, 5040).
            # 5040 is 7 hrs, 10 features, and there are 2 channels. Channel 0 is data, channel 1 is map.
            
            TEST_mixed_data_maps = torch.from_numpy(np.load(os.path.join(path, 'test_mixed_data_maps.npy')))
            TEST_mixed_labels = torch.from_numpy(np.load(os.path.join(path, 'test_mixed_labels.npy')))

            train_mixed_data_maps = torch.from_numpy(np.load(os.path.join(path, 'train_mixed_data_maps.npy')))
            train_mixed_labels = torch.from_numpy(np.load(os.path.join(path, 'train_mixed_labels.npy'))) 

    elif data_type == 'HiRID':
        window_size = learn_encoder_hyper_params['window_size']
        length_of_hour = (60*60)/300 # 60 seconds * 60 / 300 (which is num seconds in 5 min)
        pos_sample_name = 'mortality'
        path = '../DONTCOMMITdata/hirid_numpy'
        signal_list = ['vm1', 'vm3', 'vm4', 'vm5', 'vm13', 'vm20', 'vm28', 'vm62', 'vm136', 'vm146', 'vm172', 'vm174', 'vm176', 'pm41', 'pm42', 'pm43', 'pm44', 'pm87']
        sliding_gap = 1
        pre_positive_window = int((24*60*60)/300) # 24 hrs
        num_pre_positive_encodings = int(pre_positive_window/window_size)

        if DEBUG: # Smaller version of dataset to debug code with. Not representitive of the whole cohort, and test data is same as train/valid
            TEST_mixed_data_maps = torch.from_numpy(np.load(os.path.join(path, 'TEST_data_maps.npy'))).float()[0:100]
            TEST_mixed_labels = torch.from_numpy(np.load(os.path.join(path, 'TEST_labels.npy'))).float()[0:100]

            train_mixed_data_maps = torch.from_numpy(np.load(os.path.join(path, 'train_data_maps.npy'))).float()[0:150]
            train_mixed_labels = torch.from_numpy(np.load(os.path.join(path, 'train_labels.npy'))).float()[0:150]

            # set encoder cv and classifier cv to 1 so things run faster
            learn_encoder_hyper_params['n_cross_val_encoder'] = 1
            classification_hyper_params['n_cross_val_classification'] = 1

        else:
            # NOTE THE MAP CHANNEL HAS 1'S FOR OBSERVED VALUES, 0'S FOR MISSING VALUES
            # data_maps arrays are of shape (num_samples, 2, 18, 1152). 4 days of data per sample
            
            
            TEST_mixed_data_maps = torch.from_numpy(np.load(os.path.join(path, 'TEST_data_maps.npy'))).float()
            TEST_mixed_labels = torch.from_numpy(np.load(os.path.join(path, 'TEST_labels.npy'))).float()

            train_mixed_data_maps = torch.from_numpy(np.load(os.path.join(path, 'train_data_maps.npy'))).float()
            train_mixed_labels = torch.from_numpy(np.load(os.path.join(path, 'train_labels.npy'))).float()
        
    if train_encoder:
        learn_encoder(data_maps=train_mixed_data_maps, labels=train_mixed_labels, 
        encoder_type=encoder_type, encoder_hyper_params=encoder_hyper_params, 
        pretrain_hyper_params=pretrain_hyper_params, **learn_encoder_hyper_params)

    classifier_validation_aurocs = []
    classifier_validation_auprcs = []
    classifier_TEST_aurocs = []
    classifier_TEST_auprcs = []

    for encoder_cv in range(learn_encoder_hyper_params['n_cross_val_encoder']):
        for classification_cv in range(classification_hyper_params['n_cross_val_classification']):
            random.seed(123*classification_cv)
            if os.path.exists('../ckpt/%s/%s_checkpoint_%d.tar'%(data_type, UNIQUE_NAME, encoder_cv)): # i.e. a checkpoint *has* been saved for this config
                print('Loading encoder from checkpoint')
                print('Classification CV: ', classification_cv)
                print('Encoder for CV ', encoder_cv)

                checkpoint = torch.load('../ckpt/%s/%s_checkpoint_%d.tar'%(data_type, UNIQUE_NAME, encoder_cv))
                encoder = get_encoder(encoder_type, encoder_hyper_params).to(device)
                encoder.load_state_dict(checkpoint['encoder_state_dict'])
                
                
                # shuffle for this cv:
                inds = np.arange(len(train_mixed_data_maps))
                np.random.shuffle(inds)
                train_mixed_data_maps = train_mixed_data_maps[inds]
                train_mixed_labels = train_mixed_labels[inds]
                print("Size of train + valid data: ", train_mixed_data_maps.shape)
                print("Size of train + valid labels: ", train_mixed_labels.shape)

                validation_mixed_data_maps = train_mixed_data_maps[0:int(0.2*len(train_mixed_data_maps))]
                validation_mixed_labels = train_mixed_labels[0:int(0.2*len(train_mixed_data_maps))]
                print("Size of valid data: ", validation_mixed_data_maps.shape)
                print("Size of valid labels: ", validation_mixed_labels.shape)

                train_mixed_data_maps = train_mixed_data_maps[int(0.2*len(train_mixed_data_maps)):]
                train_mixed_labels = train_mixed_labels[int(0.2*len(train_mixed_labels)):]
                print("Size of train data: ", train_mixed_data_maps.shape)
                print("Size of train labels: ", train_mixed_labels.shape)


                if os.path.exists('../ckpt/%s/%s_encoder_checkpoint_%d_Classifier_checkpoint_%d.tar'%(data_type, UNIQUE_ID, encoder_cv, classification_cv)):
                    checkpoint = torch.load('../ckpt/%s/%s_encoder_checkpoint_%d_Classifier_checkpoint_%d.tar'%(data_type, UNIQUE_ID, encoder_cv, classification_cv))
                    rnn = RnnPredictor(encoding_size=encoder_hyper_params['encoding_size'], hidden_size=32).to(device)
                    # 32 is the arbitrary hidden size chosen for the RNN
                    classifier = LinearClassifier(input_size=32).to(device)
                    
                    rnn.load_state_dict(checkpoint['rnn_state_dict'])
                    classifier.load_state_dict(checkpoint['classifier_state_dict'])
                    print("Checkpoint loaded for classifier! Encoder cv %d, classifier cv %d"%(encoder_cv, classification_cv))
                else:
                    print("TRAINING LINEAR CLASSIFIER")
                    classifier_train_labels = torch.Tensor([1 in label for label in train_mixed_labels]) # Sets labels for positive samples to 1
                    classifier_validation_labels = torch.Tensor([1 in label for label in validation_mixed_labels]) # Sets labels for positive samples to 1
                    classifier_TEST_labels = torch.Tensor([1 in label for label in TEST_mixed_labels]) # Sets labels for positive samples to 1
                    

                    rnn, classifier, valid_auroc, valid_auprc, TEST_auroc, TEST_auprc = train_linear_classifier(X_train=train_mixed_data_maps, y_train=classifier_train_labels, 
                    X_validation=validation_mixed_data_maps, y_validation=classifier_validation_labels, 
                    X_TEST=TEST_mixed_data_maps, y_TEST=classifier_TEST_labels,
                    encoding_size=encoder.encoding_size, batch_size=128, num_pre_positive_encodings=num_pre_positive_encodings, encoder=encoder, return_models=True, return_scores=True, pos_sample_name=pos_sample_name, 
                    data_type=data_type, classification_cv=classification_cv, encoder_cv=encoder_cv)

                    classifier_validation_aurocs.append(valid_auroc)
                    classifier_validation_auprcs.append(valid_auprc)
                    classifier_TEST_aurocs.append(TEST_auroc)
                    classifier_TEST_auprcs.append(TEST_auprc)

            
    print("CLASSIFICATION VALIDATION RESULT OVER CV")
    print("AUC: %.2f +- %.2f, AUPRC: %.2f +- %.2f"% \
        (np.mean(classifier_validation_aurocs), 
        np.std(classifier_validation_aurocs), 
        np.mean(classifier_validation_auprcs), 
        np.std(classifier_validation_auprcs)))

    print("CLASSIFICATION TEST RESULT OVER CV")
    print("AUC: %.2f +- %.2f, AUPRC: %.2f +- %.2f"% \
        (np.mean(classifier_TEST_aurocs), 
        np.std(classifier_TEST_aurocs), 
        np.mean(classifier_TEST_auprcs), 
        np.std(classifier_TEST_auprcs)))
                    
    print("Starting encoding clustering on validation set..")

    if plot_embeddings:
        encoder.eval()
        rnn.eval()
        classifier.eval()
        indexes_chosen_to_plot = []
        print("Starting to plot embeddings..")
        num_plots = 10 # We'll plot 10 times. This number should be even
        for plot_index in range(num_plots): 
            # The first num_plots/2 will be for positive samples, and the last num_plots/2 will be for negative samples.

            ind = np.random.randint(low=0, high=len(validation_mixed_data_maps)-1)
            if plot_index < num_plots/2:
                # Selects indicies for arrest samples
                while len(torch.where(validation_mixed_labels[ind]==1)[0]) < 1 or ind in indexes_chosen_to_plot: # If there are not 1's in the labels for this sample, meaning this isn't a positive sample
                    ind = np.random.randint(low=0, high=len(validation_mixed_data_maps)-1) # Try another random sample
            
            else:
                # Selects indicies for non arrest samples
                while len(torch.where(validation_mixed_labels[ind]==1)[0]) > 0 or ind in indexes_chosen_to_plot: # If there are 1's in the labels for this sample, meaning this is a positive sample
                    ind = np.random.randint(low=0, high=len(validation_mixed_data_maps)-1) # Try another random sample
            
            
            indexes_chosen_to_plot.append(ind)

        indexes_for_clustering = indexes_chosen_to_plot.copy()
        while len(indexes_for_clustering) < 4*len(indexes_chosen_to_plot):
            ind = np.random.randint(low=0, high=len(validation_mixed_data_maps)-1)
            if ind not in indexes_for_clustering:
                indexes_for_clustering.append(ind)
        
        clustering_data_maps = torch.stack([validation_mixed_data_maps[ind] for ind in indexes_for_clustering])
        # clustering_encodings is of shape (num_samples, num_sliding_windows_per_sample, encoding_size)
        # encoding_mask is of shape (num_samples, num_sliding_windows_per_sample)
        clustering_encodings, encoding_mask = encoder.forward_seq(clustering_data_maps, return_encoding_mask=True, sliding_gap=sliding_gap)
        num_sliding_windows_per_sample = clustering_encodings.shape[1]

        pos_inds = torch.Tensor([1 in validation_mixed_labels[ind] for ind in indexes_for_clustering]).nonzero() # Tensor of indicies of positive samples
        neg_inds = torch.Tensor([1 not in validation_mixed_labels[ind] for ind in indexes_for_clustering]).nonzero() # Tensor of indicies of negative samples
        pos_clustering_encodings = clustering_encodings[pos_inds] # shape (num_pos_samples, num_sliding_windows_per_sample, encoding_size)
        neg_clustering_encodings = clustering_encodings[neg_inds] # shape (num_neg_samples, num_sliding_windows_per_sample, encoding_size
        pos_encoding_mask = encoding_mask[pos_inds] # shape (num_pos_samples, num_sliding_windows_per_sample)
        neg_encoding_mask = encoding_mask[neg_inds] # shape (num_neg_samples, num_sliding_windows_per_sample)

        clustering_encodings = clustering_encodings.reshape(-1, clustering_encodings.shape[-1])
        pos_clustering_encodings = pos_clustering_encodings.reshape(-1, pos_clustering_encodings.shape[-1])
        neg_clustering_encodings = neg_clustering_encodings.reshape(-1, neg_clustering_encodings.shape[-1])
        
        encoding_mask = encoding_mask.reshape(-1,)
        pos_encoding_mask = pos_encoding_mask.reshape(-1,)
        neg_encoding_mask = neg_encoding_mask.reshape(-1,)

        pos_inds = torch.cat([torch.arange(ind*num_sliding_windows_per_sample, ind*num_sliding_windows_per_sample + num_sliding_windows_per_sample) for ind in pos_inds]) # Now the indices are ready for the reshaped encodings
        neg_inds = torch.cat([torch.arange(ind*num_sliding_windows_per_sample, ind*num_sliding_windows_per_sample + num_sliding_windows_per_sample) for ind in neg_inds])
        

        # Only keep encodings that were not created from fully imputed data
        clustering_encodings = clustering_encodings[encoding_mask!=-1]
        pos_clustering_encodings = pos_clustering_encodings[pos_encoding_mask!=-1]
        neg_clustering_encodings = neg_clustering_encodings[neg_encoding_mask!=-1]
        pos_inds = pos_inds[pos_encoding_mask!=-1]
        neg_inds = neg_inds[neg_encoding_mask!=-1]

        clustering_model = AgglomerativeClustering(n_clusters=5).fit(clustering_encodings)

        plt.figure(figsize=(8, 5))
        plt.hist(clustering_model.labels_, bins=clustering_model.n_clusters, density=False)
        plt.ylabel('Count')
        plt.xlabel('State')
        plt.xticks(ticks=np.arange(clustering_model.n_clusters), labels=np.arange(clustering_model.n_clusters))
        plt.savefig('../DONTCOMMITplots/%s/%s/%s_mixed_cluster_label_hist.pdf'%(data_type, UNIQUE_ID, unique_name))

        
        plt.figure(figsize=(8, 5))
        plt.hist(clustering_model.labels_[neg_inds], bins=clustering_model.n_clusters, density=False)
        plt.ylabel('Count')
        plt.xlabel('State')
        plt.xticks(ticks=np.arange(clustering_model.n_clusters), labels=np.arange(clustering_model.n_clusters))
        plt.savefig('../DONTCOMMITplots/%s/%s/%s_negative_cluster_label_hist.pdf'%(data_type, UNIQUE_ID, unique_name))
        
        plt.figure(figsize=(8, 5))
        plt.hist(clustering_model.labels_[pos_inds], bins=clustering_model.n_clusters, density=False)
        plt.ylabel('Count')
        plt.xlabel('State')
        plt.xticks(ticks=np.arange(clustering_model.n_clusters), labels=np.arange(clustering_model.n_clusters))
        plt.savefig('../DONTCOMMITplots/%s/%s/%s_positive_cluster_label_hist.pdf'%(data_type, UNIQUE_ID, unique_name))


        
        # First, we'll plot embeddings of samples from the validation set, with labels from the kmeans model. This will produce a plot for arrest samples, a plot for normal samples, and a plot with mixed.
        PCA_valid_dataset_kmeans_labels(normal_encodings=neg_clustering_encodings, ca_encodings=pos_clustering_encodings, mixed_encodings=clustering_encodings,
                                        normal_cluster_labels=clustering_model.labels_[neg_inds], arrest_cluster_labels=clustering_model.labels_[pos_inds],
                                        mixed_cluster_labels=clustering_model.labels_,
                                        data_type=data_type, unique_name=UNIQUE_NAME, unique_id=UNIQUE_ID)

        
        print("Sliding Window: ", sliding_gap)


        
        normalization_specs = np.load(os.path.join(path, 'normalization_specs.npy'))
        # normalization_specs is of shape (4, num_features). First and second rows are means and stdvs for each feature for train/valid set, third and fourth rows are same but for test set
        normalization_specs = torch.Tensor(normalization_specs).to(device)


        for ind in indexes_chosen_to_plot:
            sample = validation_mixed_data_maps[ind]
            windows = []
            for i in range(0, sample.shape[2]-window_size+1, sliding_gap):
                # Note: Its assumed the sliding gap, window size, and seq len play nice (i.e. it isn't impossible for the sliding window to reach the end)
                windows.append(sample[:, :, i: i+window_size])


            with torch.no_grad():
                encodings = encoder(torch.stack(windows))
                
            encoding_batch = torch.unsqueeze(encodings, 0) # batch size of 1

            # encoding_batch is of size (1, seq_len, encoding_size) 
            
            output, _ = rnn(encoding_batch) # output contains hidden state for each time step. Shape is (batch_size, seq_len, hidden_size). batch_size=1
            output = output.squeeze() # now of shape (seq_len, hidden_size). Can be thought of as seq_len hidden states, each of size hidden_size
            risk_scores_over_time = torch.nn.Sigmoid()(classifier(output).to('cpu')).detach() # Apply sigmoid because the classifier doesn't apply this because we use BCEWITHLOGITSLOSS in train_linear_classifier


            encodings = encodings.to('cpu')
            encodings = encodings.detach().numpy().astype(np.float) # Removes gradients and converts to numpy
            labels = clustering_model.labels_
            

            # Plot risk score over time
            fig = plt.figure(figsize=(15, 6))
            ax = fig.add_subplot(1, 1, 1) # nrows, ncols, index
            ax.set_facecolor('w')
            ax.plot(np.arange(len(risk_scores_over_time)), np.array(risk_scores_over_time))
            #ax.xlabel('Time')
            ax.set_ylabel('Risk', fontsize=16)
            #ax.xlabel('Time (Hours)')
            ax.set_title('Risk Score for %s Sample'%('Normal' if plot_index >= num_plots/2 else 'Arrest'), fontsize=16)
            
            
            ax.set_xlabel('Time (Hours)', fontsize=16)
            #plt.set_xticks(np.arange(num_hours)*length_of_hour)
            ax.set_xticklabels(np.arange(7)) # 7 hrs for ICU data
        
            ax.xaxis.set_tick_params(labelsize=12)
            ax.yaxis.set_tick_params(labelsize=12)
            
            plt.savefig('../DONTCOMMITplots/%s/%s/%s_%s_risk_over_time_%d.pdf'%(data_type, UNIQUE_ID, UNIQUE_NAME, 'arrest' if plot_index < num_plots/2 else 'normal', plot_index))



            # Plot heatmap and trajectory scatter plot
            sample = sample.to(device)
            # (sample, kmeans_model, encoder, normalization_specs, path, hm_file_name, pca_file_name, device, signal_list, length_of_hour, window_size, sliding_gap)
            plot_heatmap(sample=sample, clustering_model=clustering_model, encoder=encoder, normalization_specs=normalization_specs, path='../DONTCOMMITplots/', 
            hm_file_name='%s/%s/%s_%s_trajectory_hm_%d.pdf'%(data_type, UNIQUE_ID, UNIQUE_NAME, 'CA_at_end' if plot_index < num_plots/2 else 'no_CA', plot_index), 
            device=device, signal_list=signal_list, length_of_hour=length_of_hour, window_size=window_size, sliding_gap=window_size)

            plot_pca_trajectory(sample=sample, encoder=encoder, window_size=window_size, device=device, sliding_gap=sliding_gap, kmeans_model=clustering_model, path='../DONTCOMMITplots/', pca_file_name='%s/%s/%s_%s_trajectory_embeddings_%d.pdf'%(data_type, UNIQUE_ID, UNIQUE_NAME, 'CA_at_end' if plot_index < num_plots/2 else 'no_CA', plot_index))


            sample = sample.to('cpu') # Take off GPU memory

        print("Done plotting embeddings.")
        
    

print("Finished running on ", datetime.now())
    
if __name__ == '__main__':
    print("STARTED RUNNING")
    print(device)
    # Going to update RNN
    
    print("Started running on ", datetime.now())
    parser = argparse.ArgumentParser(description='Run TNC')
    parser.add_argument('--train', action='store_true')
    parser.add_argument('--cont', action='store_true')
    parser.add_argument('--config_file', type=str, default=None)
    parser.add_argument('--checkpoint_file', type=str, default=None)
    parser.add_argument('--ID', type=str) 
    # a 4 digit number in string form. This is a unique identifier for this execution. If one wishes to find 
    # the checkpoint, plots, text output, etc for this, those will all include this ID in their name.
    
    parser.add_argument('--plot_embeddings', action='store_true')
    parser.add_argument('--DEBUG', action='store_true')
    args = parser.parse_args()
    
    STR_ID = args.ID
    DEBUG = args.DEBUG

    UNIQUE_ID = args.ID
    
    if args.config_file:
        with open(args.config_file) as c:
            config = yaml.load(c, Loader=yaml.FullLoader) # Turns config file into a Python dict

        encoder_type = config['ENCODER_TYPE']
        datatype = config['DATA_TYPE']

        UNIQUE_NAME = UNIQUE_ID + '_' + encoder_type + '_' + datatype
        print('UNIQUE_NAME: ', UNIQUE_NAME)
        encoder_hyper_params = config['ENCODER_HYPER_PARAMS'][encoder_type]
        learn_encoder_hyper_params = config['LEARN_ENCODER_HYPER_PARAMS']
        if 'device' not in encoder_hyper_params:
            encoder_hyper_params['device'] = device
        
        if 'device' not in learn_encoder_hyper_params:
            learn_encoder_hyper_params['device'] = device
        
        pretrain_hyper_params = {}
        if config['PRETRAIN'] == True:
            pretrain_hyper_params = config['PRETRAIN_HYPER_PARAMS']
        
        classification_hyper_params = config['CLASSIFICATION_HYPER_PARAMS']

        #################################################################    
        print("HYPER PARAMETERS from {}:".format(args.config_file))
        for key in config:
            print(key)
            print(config[key])
            print()
        
        main(args.train, datatype, encoder_type, encoder_hyper_params, learn_encoder_hyper_params, classification_hyper_params, args.cont, pretrain_hyper_params, args.plot_embeddings, UNIQUE_ID, UNIQUE_NAME, DEBUG=DEBUG)
    
    elif args.checkpoint_file:
        print("CHECKPOINT PASSED IN")
        checkpoint = torch.load(args.checkpoint_file)
        print('checkpoint successfully loaded!')
        main(args.train, checkpoint['data_type'], checkpoint['encoder_type'], checkpoint['encoder_hyper_params'], checkpoint['learn_encoder_hyper_params'], checkpoint['classification_hyper_params'], args.cont, checkpoint['pretrain_hyper_params'], args.plot_embeddings, checkpoint['unique_id'], checkpoint['unique_name'], DEBUG=DEBUG)




