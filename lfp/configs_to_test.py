


IMB00003 = '''
IMB0_00003
--train_dataset UR5 UR5_slow_gripper UR5_high_transition
--test_dataset UR5_slow_gripper_test
-c
-d GPU
-b 512
-la 2048
-le 512
-lp 512
-z 256
-lr 3e-4
-i
-tfr

'''.split()

B00003 = '''
GCSB0_00003
--train_dataset UR5 UR5_slow_gripper UR5_high_transition
--test_dataset UR5_slow_gripper_test
-c
-d GPU
-b 512
-la 2048
-le 512
-lp 512
-z 256
-lr 3e-4

'''.split()


PB02 = '''
PROB0_02
--train_dataset UR5 UR5_slow_gripper UR5_high_transition
--test_dataset UR5_slow_gripper_test
-c
-d GPU
-b 512
-la 2048
-le 512
-lp 512
-z 256
-lr 3e-4
-n 5

'''.split()

test4 = '''
test4
--train_datasets UR5 UR5_high_transition UR5_slow_gripper 
--test_datasets UR5_slow_gripper_test 
-tfr 
-s LOCAL 
-d GPU 
-b 32 
-la 2048 
-le 1024 
-lp 512 
-z 256 
-lr 2e-4 
-B 0.01 
-n 5 
-t 1000000 
-wmin 20 
-wmax 40 
-i 
--standard_split 32 
--lang_split 0 
--bulk_split 0 
-enc_all 
-sim Pybullet
-cnn intensities_spatial_softmax
'''.split()


B0003 = '''
GCSB0_0003
--train_dataset UR5 UR5_slow_gripper UR5_high_transition
--test_dataset UR5_slow_gripper_test
-c
-d GPU
-b 512
-la 2048
-le 512
-lp 512
-z 256
-lr 3e-4

'''.split()


B000003 = '''
GCSB0_000003
--train_dataset UR5 UR5_slow_gripper UR5_high_transition
--test_dataset UR5_slow_gripper_test
-c
-d GPU
-b 512
-la 2048
-le 512
-lp 512
-z 256
-lr 3e-4

'''.split()
