with candidates as (
 select c.comment_id,c.note_id,c.content as comment_text,c.comment_role,n.title,n.raw_content,n.project_id,p.category,
 row_number() over(partition by c.project_id,c.content order by c.comment_id) as duplicate_rank
 from truth_vault.comments c join truth_vault.notes n on n.note_id=c.note_id
 join truth_vault.projects p on p.project_id=n.project_id
 where p.category in ('OTC药','保健品','处方药') and c.is_displayed is true
 and length(btrim(c.content))>=3 and length(btrim(n.raw_content))>=50
), per_post as (
 select *,row_number() over(partition by note_id order by md5(comment_id||'J03-20261012')) as note_rank
 from candidates where duplicate_rank=1
), per_role as (
 select *,row_number() over(partition by category,comment_role='贴主' order by md5(comment_id||'J03-role')) as role_rank
 from per_post where note_rank<=3
), per_group as (
 select *,row_number() over(partition by category order by (comment_role='贴主') desc,md5(comment_id||'J03-group')) as category_rank
 from per_role where comment_role<>'贴主' or role_rank<=5
) select comment_id,note_id,comment_text,comment_role,title,raw_content,project_id,category
 from per_group where category_rank<=case category when 'OTC药' then 35 when '保健品' then 35 else 30 end
 order by category,category_rank;
