import os
import sys
project_path = '../..'
sys.path.append(project_path)
import getpass
import ntpath
import logging
import time
logging.basicConfig(level=logging.DEBUG, filename="LOG_Triage.debug", filemode='w')
logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
from datetime import datetime
import yaml
from triage import create_engine
import pandas as pd
import shutil
from triage.component.catwalk.storage import ProjectStorage, ModelStorageEngine, MatrixStorageEngine

'''
Works as follows:
- Gets train matrices from an experiment id.
- Choose ratio
- Modifies the train matrix in certain way and inserts it into different dir structures.
- Undersampling bruh.
'''
def read_config_file(config_file):
    config = None
    try:
        with open (config_file, 'r') as file:
            config = yaml.safe_load(file)
    except Exception as e:
        print(e)
        print('Error reading the config file')
    return config

def connect(cred_folder):
    cred_file = os.path.join(cred_folder, 'donors_db_profile.yaml')
    db = read_config_file(cred_file)

    sql_engine = create_engine(
        'postgresql+psycopg2://%s:%s@%s:%i/%s'%(
            db['user'],
            db['pass'],
            db['host'],
            db['port'],
            db['db']
        )
    )
    return sql_engine

def get_entities_attribute(conn, config):
    entity_to_attrib = {}
    query = """
    SELECT entity_id, %s 
    FROM %s
    """%(str(config['demo_col']), str(config['demo_table']))

    df = pd.read_sql(query, conn)
    entities = df['entity_id'].values
    attribs = df[config['demo_col']].values
    
    for i in range(len(entities)):
        entity_id = entities[i]
        attrib_info = attribs[i]
        entity_to_attrib[entity_id] = attrib_info
    
    return entity_to_attrib

def get_test_matrices_ids(conn, experiment_id):
    query = """
    SELECT matrix_uuid from model_metadata.matrices where matrix_type='test' AND
    built_by_experiment = '%s'
    """%(str(experiment_id))

    df = pd.read_sql(query, conn)
    test_matrices = df['matrix_uuid'].values

    return test_matrices

def get_train_matrices_ids(conn, experiment_id):
    query = """
    SELECT matrix_uuid from model_metadata.matrices where matrix_type='train' AND built_by_experiment = '%s'
    """%(str(experiment_id))

    df = pd.read_sql(query, conn)
    train_matrices = df['matrix_uuid'].values

    return train_matrices

def read_train_matrix(matrix_id, config, entity_to_attrib):
    matrix_data = pd.read_csv(os.path.join(config['orig_data_dir'], str(matrix_id)+'.csv.gz'),compression = 'gzip', error_bad_lines=False)

    entities = matrix_data['entity_id'].values
    demo_col_arr = []
    missed = 0
    
    for i in range(len(entities)):
        entity_id = entities[i]
        try:
            demo_col_info = entity_to_attrib[entity_id]
            demo_col_arr.append(demo_col_info)
        except KeyError as e:
            missed = missed + 1
            demo_col_arr.append('MISSING')

    matrix_data[config['demo_col']] = demo_col_arr
    matrix_data = matrix_data[matrix_data[config['demo_col']]!='MISSING']
    
    attrib_1 = config['demo_permutations'][0]
    attrib_2 = config['demo_permutations'][1]

    attrib_1_df = matrix_data[matrix_data[config['demo_col']] == attrib_1]
    attrib_2_df = matrix_data[matrix_data[config['demo_col']] == attrib_2]

    return attrib_1_df, attrib_2_df

def print_matrix_stats(merged_df, config):
    print("="*20)
    print("Matrix Stats")
    print("="*20)
    print(pd.value_counts(merged_df[config['demo_col']]))

    for demo_val in config['demo_permutations']:
        for label in [0,1]:
            print("DemoVal="+str(demo_val)+"\t\tLabel="+str(label))
            print(len(merged_df[(merged_df[config['demo_col']]==demo_val) & (merged_df[config['label']]==label)]))
    print("="*30)

def undersample(train_matrix_ids, config, entity_to_attrib):   
    for matrix_id in train_matrix_ids:
        print("MATRIX ID:"+str(matrix_id))
        value_1_df, value_2_df = read_train_matrix(matrix_id, config,entity_to_attrib)
        
        if(len(value_1_df) > len(value_2_df)):
            larger_df = value_1_df
            smaller_df = value_2_df
        else:
            larger_df = value_2_df
            smaller_df = value_1_df
            
        #if ratio is 1:1, sample n_small from larger_df
        for i in range(config['N']):
            if config['ratio'] * len(smaller_df) <= len(larger_df):
                sampled_larger_df = larger_df.sample(n=config['ratio']*len(smaller_df))
                merged_df = pd.concat([smaller_df, sampled_larger_df])
                print(len(smaller_df), len(sampled_larger_df))
            else:
                # sample len(larger_df)/ratio from smaller_df
                sampled_smaller_df = smaller_df.sample(n = int(len(larger_df)/config['ratio']))
                merged_df = pd.concat([sampled_smaller_df, larger_df])
                print(len(sampled_smaller_df), len(larger_df))
        
            us_dest_data_dir = os.path.join(config['dest_data_dir'], 'undersampled_'+str(i), 'matrices')

            if not os.path.exists(us_dest_data_dir):
                os.makedirs(us_dest_data_dir)
        
            print_matrix_stats(merged_df, config)

            filtered_df = merged_df.drop(config['demo_col'], 1)
            filtered_df.to_csv(os.path.join(us_dest_data_dir, str(matrix_id)+".csv.gz"), compression='gzip', index=False)

def undersample_maintain_subgroup(train_matrix_ids, config, entity_to_attrib):
    for matrix_id in train_matrix_ids:
        value_1_df, value_2_df = read_train_matrix(matrix_id, config, entity_to_attrib)

        # PROCESS white_df
        value_1_label_0_df = value_1_df[value_1_df[config['label']]==0.0]
        value_1_label_1_df = value_1_df[value_1_df[config['label']]==1.0]

        value_2_label_0_df = value_2_df[value_2_df[config['label']]==0.0]
        value_2_label_1_df = value_2_df[value_2_df[config['label']]==1.0]
        
        for i in range(config['N']):
            if(len(value_1_label_0_df) <  len(value_1_label_1_df)):
                # sample some from white_1
                sampled_value1_label1_df = value_1_label_1_df.sample(n=len(value_1_label_0_df))
                merged_value1_df = pd.concat([value_1_label_0_df, sampled_value1_label1_df])
            else:
                sampled_value1_label0_df = value_1_label_0_df.sample(n=len(value_1_label_1_df))
                merged_value1_df = pd.concat([sampled_value1_label0_df, value_1_label_1_df])

        
            # PROCESS non_white_df    
            if(len(value_2_label_0_df) < len(value_2_label_1_df)):
                sampled_value2_label1_df = value_2_label_1_df.sample(n=len(value_2_label_0_df))
                merged_value2_df = pd.concat([value2_label_0_df, sampled_value2_label1_df])
                
            else:
                sampled_value2_label0_df = value_2_label_0_df.sample(n=len(value_2_label_1_df))
                merged_value2_df = pd.concat([sampled_value2_label0_df, value_2_label_1_df])
                

            us_dest_data_dir = os.path.join(config['dest_data_dir'], 'undersampled_frac_'+str(i), 'matrices')
            if not os.path.exists(us_dest_data_dir):
                os.makedirs(us_dest_data_dir)
        
            merged_df = pd.concat([merged_value1_df, merged_value2_df])
            print_matrix_stats(merged_df, config)
            
            filtered_df = merged_df.drop(config['demo_col'], 1)
            filtered_df.to_csv(os.path.join(us_dest_data_dir, str(matrix_id)+".csv.gz"), compression='gzip', index=False)

def undersample_multiple_times(conn, config, entity_to_attrib):
    train_matrix_ids = get_train_matrices_ids(conn, config['experiment_hash'])
    print("NUMBER OF MATRICES="+str(len(train_matrix_ids)))
    
    if(config['mode']==1):
        undersample(train_matrix_ids, config, entity_to_attrib)
    if(config['mode']==2):
        undersample_maintain_subgroup(train_matrix_ids, config, entity_to_attrib)

def copy_test_yaml(conn, config):
    test_matrix_ids = get_test_matrices_ids(conn, config['experiment_hash'])
    
    for matrix_id in test_matrix_ids:
        matrix_file_name = os.path.join(config['orig_data_dir'], str(matrix_id)+'.csv.gz')
        for i in range(config['N']):
            if config['mode']==1:
                dest_file_name = os.path.join(config['dest_data_dir'], 'undersampled_'+str(i), 'matrices', str(matrix_id)+'.csv.gz')
            elif config['mode']==2:
                dest_file_name = os.path.join(config['dest_data_dir'], 'undersampled_frac_'+str(i), 'matrices', str(matrix_id)+'.csv.gz')
            
            shutil.copy(matrix_file_name, dest_file_name)

    print("Orig data dir="+str(os.path.join(config['orig_data_dir'])))
    
    for in_file in os.listdir(os.path.join(config['orig_data_dir'])):
        if(in_file.endswith("yaml")):
            for i in range(config['N']):
                orig_filename = os.path.join(config['orig_data_dir'], in_file)
                if config['mode']==1:
                    dest_filename = os.path.join(config['dest_data_dir'], 'undersampled_'+str(i), 'matrices', in_file)
                    shutil.copy(orig_filename, dest_filename)
                elif config['mode']==2:
                    dest_filename = os.path.join(config['dest_data_dir'], 'undersampled_frac_'+str(i), 'matrices', in_file)
                    shutil.copy(orig_filename, dest_filename)

if __name__ == '__main__':
    conn = connect('../../config')
    file_path = sys.argv[1]
    config_file = sys.argv[2]
    config = read_config_file(config_file)
    
    config['orig_data_dir'] = os.path.join(file_path, config['orig_data_name'], 'matrices')
    config['dest_data_dir'] = os.path.join(file_path, config['dest_data_name'])

    print("CONFIG")
    print(config)
    print("="*10)
    
    entity_to_attrib = get_entities_attribute(conn, config)
    undersample_multiple_times(conn, config, entity_to_attrib)
    copy_test_yaml(conn, config)