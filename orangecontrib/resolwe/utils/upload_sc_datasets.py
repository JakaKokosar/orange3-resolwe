import os
import urllib.request
import json


from concurrent.futures import ThreadPoolExecutor
from resdk import Resolwe
from Orange.data.table import Table

URL_REMOTE = 'http://datasets.orange.biolab.si/sc/'
SC_FILES = [
    # ('DC_expMatrix_DCnMono.tab.gz', '9606'),
    # ('DC_expMatrix_deeper.characterization.tab.gz', '9606'),
    ('aml-1k.pickle', '9606'),
    ('aml-8k.pickle', '9606'),
    # ('ccp_data_Tcells_normCounts.counts.all_genes.tab.gz', '10090'),
    # ('ccp_data_Tcells_normCounts.counts.cycle_genes.tab.gz', '10090'),
    # ('ccp_data_liver.counts.all_genes.tab.gz', '10090'),
    # ('ccp_data_liver.counts.cycle_genes.tab.gz', '10090'),
    ('ccp_data_mESCbulk.counts.all_genes.tab.gz', '10090'),
    ('ccp_data_mESCbulk.counts.cycle_genes.tab.gz', '10090'),
    ('ccp_normCountsBuettnerEtAl.counts.all_genes.tab.gz', '10090'),
    ('ccp_normCountsBuettnerEtAl.counts.cycle_genes.tab.gz', '10090'),
    ('ccp_normCounts_mESCquartz.counts.all_genes.tab.gz', '10090'),
    ('ccp_normCounts_mESCquartz.counts.cycle_genes.tab.gz', '10090'),
    # ('cdp_expression_macosko.tab.gz', '10090'),
    ('cdp_expression_shekhar.tab.gz', '10090')
]


def upload(filename):
    annotations = {'tabular': {},
                   'other': {}}

    with urllib.request.urlopen(os.path.join(URL_REMOTE, filename + '.info')) as url:
        data = json.loads(url.read().decode())
        annotations['tabular']['title'] = data['title']
        annotations['tabular']['cells'] = data['instances']
        annotations['tabular']['genes'] = data['num_of_genes']
        annotations['tabular']['tax_id'] = data['taxid']
        annotations['tabular']['target'] = data['target'] if data['target'] else ''
        annotations['tabular']['tags'] = ', '.join(data['tags'])
        annotations['other']['description'] = data['description']
        annotations['other']['references'] = ' | '.join(data['references'])
        annotations['other']['source'] = data['source']
        annotations['other']['collection'] = data['collection']
        annotations['other']['year'] = data['year']
        annotations['other']['instances'] = data['instances']
        annotations['other']['variables'] = data['variables']

    data = Table(os.path.join(URL_REMOTE, filename))

    if '.tab.gz' in filename:
        filename = filename.replace('.tab.gz', '.pickle')

    data.save(filename)

    dataset = res.run(
        'data-table-upload',
        input={'src': filename}
    )

    # dataset = res.data.get(id=1)
    annotations['tabular']['file_name'] = filename
    annotations['tabular']['file_size'] = os.stat(filename).st_size

    # descriptor schema slug
    dataset.descriptor_schema = 'data_info'

    dataset.descriptor = annotations
    dataset.save()

    # cleanup
    os.remove(filename)


if __name__ == '__main__':
    res = Resolwe('admin', 'admin123', 'http://127.0.0.1:8000/')
    # upload('aml-1k.pickle')

    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(upload, sc_file[0]) for sc_file in SC_FILES]
