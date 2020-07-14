import pandas as pd
import numpy as np
import yaml
import xarray as xr
import datetime
from dateutil.relativedelta import relativedelta


def scale(data_arr, std=None, mean=None):
    """
    scale the data so it has a standard deviation of 1 and a mean of zero
    :param data_arr: [numpy array] input or output data with dims
    [nseg, ndates, nfeats]
    :param std: [numpy array] standard deviation if scaling test data with dims
    [nfeats]
    :param mean: [numpy array] mean if scaling test data with dims [nfeats]
    :return: scaled data with original dims
    """
    if not isinstance(std, xr.Dataset) or not isinstance(mean, xr.Dataset):
        std = data_arr.std(skipna=True)
        mean = data_arr.mean(skipna=True)
    # adding small number in case there is a std of zero
    scaled = (data_arr - mean) / (std + 1e-10)
    check_if_finite(std)
    check_if_finite(mean)
    return scaled, std, mean


def separate_trn_tst(dataset, test_start, n_test_years):
    """
    separate the train data from the test data according to the start and end
    dates. This assumes your training data is in one continuous block and all
    the dates that are not in the training are in the testing.
    :param dataset: [xr dataset] input or output data with dims
    :param test_start: [str] date where training data should start
    :param n_test_years: [int] number of years to take for the test period
    """
    start_date = datetime.datetime.strptime(test_start, '%Y-%m-%d')
    test_end = start_date + relativedelta(years=n_test_years)
    tst = dataset.sel(date=slice(test_start, test_end))
    # take all the rest
    trn_dates = dataset.date[~dataset.date.isin(tst.date)]
    trn = dataset.sel(date=trn_dates)
    return trn, tst


def split_into_batches(data_array, seq_len=365, offset=1):
    """
    split training data into batches with size of batch_size
    :param data_array: [numpy array] array of training data with dims [nseg,
    ndates, nfeat]
    :param seq_len: [int] length of sequences (i.e., 365)
    :param offset: [float] 0-1, how to offset the batches (e.g., 0.5 means that
    the first batch will be 0-365 and the second will be 182-547)
    :return: [numpy array] batched data with dims [nbatches, nseg, seq_len
    (batch_size), nfeat]
    """
    combined = []
    for i in range(int(1 / offset)):
        start = int(i * offset * seq_len)
        idx = np.arange(start=start, stop=data_array.shape[1] + 1,
                        step=seq_len)
        split = np.split(data_array, indices_or_sections=idx, axis=1)
        # add all but the first and last batch since they will be smaller
        combined.extend([s for s in split if s.shape[1] == seq_len])
    combined = np.asarray(combined)
    return combined


def get_unique_dates(partition, x_data_file):
    """
    get the unique dates for a partition
    :param partition: [str] 'tst', 'trn', or 'both'
    :param x_data_file: [str] path to x_data_file
    :return: [np array] unique dates
    """
    return np.sort(np.unique(np.load(x_data_file)[f'dates_{partition}']))


def get_dates(partition, x_data_file):
    """
    get the dates for a certain partition
    :param partition: [str] 'tst', 'trn', or 'both'
    :param x_data_file: [str] path to x_data_file
    :return: [array] dates
    """
    if partition == 'both':
        trn_dates = get_unique_dates('trn', x_data_file)
        tst_dates = get_unique_dates('tst', x_data_file)
        return np.sort(np.concatenate([trn_dates, tst_dates]))
    else:
        return get_unique_dates(partition, x_data_file)


def read_multiple_obs(obs_files, pre_train_file):
    """
    read and format multiple observation files. we read in the pretrain data to
    make sure we have the same indexing.
    :param obs_files: [list] list of filenames of observation files
    :param pre_train_file: [str] the file of pre_training data
    :return: [xr dataset] the observations in the same time
    """
    obs = [xr.open_zarr(pre_train_file).sortby(['seg_id_nat', 'date'])]
    for filename in obs_files:
        ds = xr.open_zarr(filename)
        obs.append(ds)
    obs = xr.merge(obs, join='left')
    obs = obs[['temp_c', 'discharge_cms']]
    obs = obs.rename({'temp_c': 'seg_tave_water',
                      'discharge_cms': 'seg_outflow'})
    return obs


def join_catch_properties(x_data_ts, catch_props):
    """
    append the catchment properties to the x time series data
    :param x_data_ts: [xr dataset] timeseries x-data
    :param catch_props: [xr dataset] catchment properties data
    :return: [xr dataset] the merged datasets
    """
    # broadcast the catchment properties on the ts data so that there is a value
    # for each date
    _, ds_catch = xr.broadcast(x_data_ts, catch_props)
    return xr.merge([x_data_ts, ds_catch], join='left')


def prep_catch_props(x_data_ts, catch_prop_file):
    """
    read catch property file and join with ts data
    :param x_data_ts: [xr dataset] timeseries x-data
    :param catch_prop_file: [str] the feather file of catchment attributes
    :return: [xr dataset] merged datasets
    """
    df_catch_props = pd.read_feather(catch_prop_file)
    ds_catch_props = df_catch_props.set_index('seg_id_nat').to_xarray()
    return join_catch_properties(x_data_ts, ds_catch_props)


def reshape_for_training(data):
    """
    reshape the data for training
    :param data: training data (either x or y or mask) dims: [nbatch, nseg,
    len_seq, nfeat/nout]
    :return: reshaped data [nbatch * nseg, len_seq, nfeat/nout]
    """
    n_batch, n_seg, seq_len, n_feat = data.shape
    return np.reshape(data, [n_batch * n_seg, seq_len, n_feat])


def exclude_segments(weights, exclude_segs):
    """
    exclude segments from being trained on by setting their weights as zero
    :param weights: [xr dataset] dataset of weights
    :param exclude_segs: [list] list of segments to exclude in the loss
    calculation
    :return:
    """
    for seg_grp in exclude_segs:
        start = seg_grp.get('start_date')
        if start:
            start = datetime.datetime.strptime(start, '%Y-%m-%d')

        end = seg_grp.get('end_date')
        if end:
            end = datetime.datetime.strptime(end, '%Y-%m-%d')

        for v in weights.data_vars:
            weights[v].load()
            if 'seg_id_nats_ex' in seg_grp.keys():
                ex_segs = seg_grp['seg_id_nats_ex']
            elif 'seg_id_nats_in' in seg_grp.keys():
                ex_mask = ~weights.seg_id_nat.isin(seg_grp['seg_id_nats_in'])
                ex_segs = weights.seg_id_nat[ex_mask]
            else:
                raise ValueError('exclude grp needs either "seg_id_nats_in" or'
                                 '"seg_id_nats_ex')
            weights[v].loc[ex_segs, start:end] = 0
    return weights


def initialize_weights(y_data, initial_val=1):
    """
    initialize all weights with a value.
    :param y_data:[xr dataset] y data. this is used to get the dimensions
    :param initial_val: [num] a number to initialize the weights with. should
    be between 0 and 1 (inclusive)
    :return: [xr dataset] dataset weights initialized with a uniform value
    """
    weights = y_data.copy(deep=True)
    for v in y_data.data_vars:
        weights[v].load()
        weights[v].loc[:, :] = initial_val
    return weights


def change_weights_by_outcols(weights, out_cols):
    """
    modify the weights by the outcolumns
    :param weights:[xr dataset] weights
    :param out_cols:[str] which columns you will be looking at. should be
    'temp', 'flow', or 'both'
    :returns: [xr dataset] modified weights
    """
    if out_cols == "both":
        pass
    elif out_cols == 'temp':
        weights.seg_outflow.loc[:, :] = 0
    elif out_cols == 'flow':
        weights.seg_tave_water.loc[:, :] = 0
    else:
        raise ValueError('out_cols needs to be "flow", "temp", or "both"')
    return weights


def create_weight_vectors(y_data, out_cols, exclude_segs):
    """
    filter out either flow, temperature, or neither in the pre-training and
    finetune y data
    :param y_data: [xr dataset]
    :param out_cols: [str] which columns to have count in the loss function;
    either 'temp', 'flow', or 'both'
    :param exclude_segs: [list] list of segment ids to exclude from the loss
    function
    :return: [xr dataset] dataset of weights between one and zero
    """
    initial_weights = initialize_weights(y_data, 1)
    weights = change_weights_by_outcols(initial_weights, out_cols)
    if exclude_segs:
        weights = exclude_segments(weights, exclude_segs)
    return weights


def convert_batch_reshape(dataset):
    """
    convert xarray dataset into numpy array, swap the axes, batch the array and
    reshape for training
    :param dataset: [xr dataset] x or y data
    :return: [numpy array] batched and reshaped dataset
    """
    # convert xr.dataset to numpy array
    dataset = dataset.transpose('seg_id_nat', 'date')
    arr = dataset.to_array().values

    # before [nfeat, nseg, ndates]; after [nseg, ndates, nfeat]
    # this is the order that the split into batches expects
    arr = np.moveaxis(arr, 0, -1)

    # batch the data
    # after [nbatch, nseg, seq_len, nfeat]
    batched = split_into_batches(arr)

    # reshape data
    # after [nbatch * nseg, seq_len, nfeat]
    reshaped = reshape_for_training(batched)
    return reshaped


def coord_as_reshaped_array(dataset, coord_name):
    coord_array = xr.broadcast(dataset[coord_name], dataset['seg_tave_air'])[0]
    new_var_name = coord_name + '1'
    dataset[new_var_name] = coord_array
    reshaped_np_arr = convert_batch_reshape(dataset[[new_var_name]])
    return reshaped_np_arr


def check_if_finite(xarr):
    assert np.isfinite(xarr.to_array().values).all()


def log_discharge(y):
    """
    take the log of discharge
    :param y: [xr dataset] the y data
    :return: [xr dataset] the data logged
    """
    y['seg_outflow'].loc[:, :] = y['seg_outflow'] + 1e-6
    y['seg_outflow'].loc[:, :] = xr.ufuncs.log(y['seg_outflow'])
    return y


def get_y_partition(ds_y, x_data_file, partition):
    """
    get the parition for a y dataset
    :param ds_y: [xr dataset] an xarray dataset of the y
    :param x_data_file: [str] path to x_data_file
    :param partition: [str] 'trn' or 'tst'
    :return: partitioned data
    """
    dates = get_unique_dates(partition, x_data_file)
    return ds_y.sel(date=dates)


def get_y_obs(obs_files, pretrain_file, finetune_vars):
    """
    get y_obs_trn and y_obs_tst
    :param obs_files: [list] observation files
    :param pretrain_file: [str] path to pretrain file
    :param finetune_vars: [list] variables that will be used in finetuning
    :return: [xr datasets]
    """
    ds_y_obs = read_multiple_obs(obs_files, pretrain_file)
    ds_y_obs = ds_y_obs[finetune_vars]
    return ds_y_obs


def prep_data(obs_temper_file, obs_flow_file, pretrain_file, distfile, x_vars,
              catch_prop_file=None, pretrain_vars='both', finetune_vars='both',
              test_start_date='2004-09-30', n_test_yr=12, exclude_file=None,
              log_q=False, out_file=None):
    """
    prepare input and output data for DL model training read in and process
    data into training and testing datasets. the training and testing data are
    scaled to have a std of 1 and a mean of zero
    :param obs_temper_file: [str] temperature observations file (csv)
    :param obs_flow_file:[str] discharge observations file (csv)
    :param pretrain_file: [str] the file with the pretraining data (SNTemp data)
    :param distfile: [str] path to the distance matrix .npz file
    :param x_vars: [list] variables that should be used as input
    :param catch_prop_file: [str] the path to the catchment properties file. If
    left unfilled, the catchment properties will not be included as predictors
    :param pretrain_vars: [str] 'flow', 'temp', or 'both'
    :param finetune_vars: [str] 'flow', 'temp', or 'both'
    :param test_start_date: [str] the date to start for the test period
    :param n_test_yr: [int] number of years to take for the test period
    :param exclude_file: [str] path to exclude file
    :param log_q: [bool] whether or not to take the log of discharge in training
    :param out_file: [str] file to where the values will be written
    :returns: training and testing data along with the means and standard
    deviations of the training input and output data
            'y_trn_pre': batched, scaled, and centered output data for entire
                         period of record of SNTemp [n_samples, seq_len, n_out]
            'y_obs_trn': batched, scaled, and centered output observation data
                         for the training period
            'y_trn_obs_std': standard deviation of the y observations training
                             data [n_out]
            'y_trn_obs_mean': mean of the observation training data [n_out]
            'y_obs_tst': un-batched, unscaled, uncentered observation data for
                         the test period [n_yrs, n_seg, len_seq, n_out]
            'dates_ids_trn: batched dates and national seg ids for training data
                            [n_samples, seq_len, 2]
            'dates_ids_tst: un-batched dates and national seg ids for testing
                            data [n_yrs, n_seg, len_seq, 2]
    """

    ds_pre = xr.open_zarr(pretrain_file)
    x_data = ds_pre[x_vars]
    if catch_prop_file:
        x_data = prep_catch_props(x_data, catch_prop_file)
    x_trn, x_tst = separate_trn_tst(x_data, test_start_date, n_test_yr)

    x_scl, x_std, x_mean = scale(x_data)

    x_trn_scl, _, _ = scale(x_trn, std=x_std, mean=x_mean)
    x_tst_scl, _, _ = scale(x_tst, std=x_std, mean=x_mean)

    # read, filter observations for finetuning
    y_obs = read_multiple_obs([obs_temper_file, obs_flow_file], pretrain_file)
    y_obs_trn, y_obs_tst = separate_trn_tst(y_obs, test_start_date, n_test_yr)
    y_vars = ['seg_tave_water', 'seg_outflow']
    y_pre = ds_pre[y_vars]
    y_pre_trn, _ = separate_trn_tst(y_pre, test_start_date, n_test_yr)

    if log_q:
        y_obs_trn = log_discharge(y_obs_trn)
        y_pre_trn = log_discharge(y_pre_trn)

    # filter pretrain/finetune y
    if exclude_file:
        exclude_segs = read_exclude_segs_file(exclude_file)
    else:
        exclude_segs = None
    y_pre_wgts = create_weight_vectors(y_pre_trn, pretrain_vars,
                                       exclude_segs=None)
    y_obs_wgts = create_weight_vectors(y_pre_trn, finetune_vars,
                                       exclude_segs=exclude_segs)

    # scale y training data and get the mean and std
    y_trn_obs_scl, y_trn_obs_std, y_trn_obs_mean = scale(y_obs_trn)
    # for pre-training, keep everything together
    y_trn_pre_scl, _, _ = scale(y_pre_trn)

    data = {'x_trn': convert_batch_reshape(x_trn_scl),
            'x_tst': convert_batch_reshape(x_tst_scl),
            'x_std': x_std.to_array().values,
            'x_mean': x_mean.to_array().values,
            'x_cols': np.array(x_vars),
            'ids_trn': coord_as_reshaped_array(x_trn, 'seg_id_nat'),
            'dates_trn': coord_as_reshaped_array(x_trn, 'date'),
            'ids_tst': coord_as_reshaped_array(x_tst, 'seg_id_nat'),
            'dates_tst': coord_as_reshaped_array(x_tst, 'date'),
            'y_pre_trn': convert_batch_reshape(y_trn_pre_scl),
            'y_obs_trn': convert_batch_reshape(y_trn_obs_scl),
            'y_obs_trn_std': y_trn_obs_std.to_array().values,
            'y_obs_trn_mean': y_trn_obs_mean.to_array().values,
            'y_pre_wgts': convert_batch_reshape(y_pre_wgts),
            'y_obs_wgts': convert_batch_reshape(y_obs_wgts),
            'y_vars': np.array(y_vars),
            'y_obs_tst': convert_batch_reshape(y_obs_tst),
            'dist_matrix': prep_adj_matrix(distfile, 'upstream')
            }
    if out_file:
        np.savez_compressed(out_file, **data)
    return data


def sort_dist_matrix(mat, row_col_names):
    """
    sort the distance matrix by seg_id_nat
    :return:
    """
    df = pd.DataFrame(mat, columns=row_col_names, index=row_col_names)
    df = df.sort_index(axis=0)
    df = df.sort_index(axis=1)
    return df


def prep_adj_matrix(infile, dist_type, out_file=None):
    """
    process adj matrix.
    **The resulting matrix is sorted by seg_id_nat **
    :param infile:
    :param dist_type: [str] type of distance matrix ("upstream", "downstream" or
    "updown")
    :param out_file:
    :return: [numpy array] processed adjacency matrix
    """
    adj_matrices = np.load(infile)
    adj = adj_matrices[dist_type]
    adj = sort_dist_matrix(adj, adj_matrices['rowcolnames'])
    adj = np.where(np.isinf(adj), 0, adj)
    adj = -adj
    mean_adj = np.mean(adj[adj != 0])
    std_adj = np.std(adj[adj != 0])
    adj[adj != 0] = adj[adj != 0] - mean_adj
    adj[adj != 0] = adj[adj != 0] / std_adj
    adj[adj != 0] = 1 / (1 + np.exp(-adj[adj != 0]))

    I = np.eye(adj.shape[0])
    A_hat = adj.copy() + I
    D = np.sum(A_hat, axis=1)
    D_inv = D ** -1.0
    D_inv = np.diag(D_inv)
    A_hat = np.matmul(D_inv, A_hat)
    if out_file:
        np.savez_compressed(out_file, dist_matrix=A_hat)
    return A_hat


def read_exclude_segs_file(exclude_file):
    """
    read the exclude segs file. should be a yml file with start_date and list of
    segments to exclude
    --
    example exclude file:

    group_after_2017:
        start_date: "2017-10-01"
        seg_id_nats:
            - 1556
            - 1569
    group_2018_water_year:
        start_date: "2017-10-01"
        end_date: "2018-10-01"
        seg_id_nats:
            - 1653
    group_all_time:
        seg_id_nats:
            - 1806
            - 2030

    --
    :param exclude_file: [str] exclude segs file
    :return: [list] list of dictionaries of segments to exclude. dict keys must
    have 'seg_id_nats' and may also have 'start_date' and 'end_date'
    """
    with open(exclude_file, 'r') as s:
        d = yaml.safe_load(s)
    return [val for key, val in d.items()]