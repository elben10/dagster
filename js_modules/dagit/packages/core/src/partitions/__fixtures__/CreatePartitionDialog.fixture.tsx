import {MockedResponse} from '@apollo/client/testing';

import {CREATE_PARTITION_MUTATION} from '../CreatePartitionDialog';
import {AddDynamicPartitionMutation} from '../types/CreatePartitionDialog.types';

export function buildCreatePartitionFixture({
  partitionsDefName,
  partitionKey,
}: {
  partitionsDefName: string;
  partitionKey: string;
}): MockedResponse<AddDynamicPartitionMutation> {
  return {
    request: {
      query: CREATE_PARTITION_MUTATION,
      variables: {
        partitionsDefName,
        partitionKey,
        repositorySelector: {
          repositoryLocationName: 'testing',
          repositoryName: 'testing',
        },
      },
    },
    result: jest.fn(() => ({
      data: {
        __typename: 'DagitMutation',
        addDynamicPartition: {
          __typename: 'AddDynamicPartitionSuccess',
          partitionsDefName: partitionKey,
          partitionKey,
        },
      },
    })),
  };
}
