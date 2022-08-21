## without language
````
CUDA_VISIBLE_DEVICES=6 taskset -c 1,2,3,4,5,6,7,8,9,10 python lfp_zhk.py test3 --train_datasets UR5 UR5_high_transition UR5_slow_gripper --test_datasets UR5_slow_gripper_test -tfr -s LOCAL -d GPU -b 64 -la 2048 -le 1024 -lp 512 -z 256 -lr 3e-4 -B 0.01 -n 5 -t 50000 -wmin 20 -wmax 40 -i --standard_split 64 --lang_split 0 --bulk_split 0 -enc_all -sim Pybullet -if test3
````

## with language
````
CUDA_VISIBLE_DEVICES=6 taskset -c 1,2,3,4,5,6,7,8,9,10 python lfp_zhk.py test3 --train_datasets UR5 UR5_high_transition UR5_slow_gripper --test_datasets UR5_slow_gripper_test -tfr -s LOCAL -d GPU -b 64 -la 2048 -le 1024 -lp 512 -z 256 -lr 3e-4 -B 0.01 -n 5 -t 50000 -wmin 20 -wmax 40 -i --standard_split 60 --lang_split 4 --bulk_split 0 -enc_all -sim Pybullet -if test3 -lang 
````

## intensities spatial softmax cnn
````
## without language
## 
CUDA_VISIBLE_DEVICES=6 taskset -c 1,2,3,4,5,6,7,8,9,10 python lfp_zhk.py test4 --train_datasets UR5 UR5_high_transition UR5_slow_gripper --test_datasets UR5_slow_gripper_test -tfr -s LOCAL -d GPU -b 64 -la 2048 -le 1024 -lp 512 -z 256 -lr 3e-4 -B 0.01 -n 5 -t 50000 -wmin 20 -wmax 40 -i --standard_split 64 --lang_split 0 --bulk_split 0 -enc_all -sim Pybullet -cnn intensities_spatial_softmax -if test4
````