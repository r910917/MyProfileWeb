title "����"
@echo off
REM ���ʨ�M�׸�Ƨ��]�Ч令�A�ۤv�����|�^
cd /d C:\Users\User\venv\mysite

REM �[�J�Ҧ��ɮ�
git add .

REM �۰ʫإ� commit �T���]�ɶ��W�O�^
set datetime=%date% %time%
git commit -m "Auto commit at %datetime%"

REM ���e�� GitHub master ����
git push origin master

pause