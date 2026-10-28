import { useRef, useState } from 'react'
import type { ChangeEvent } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import type { HomeEntity } from '../../types/api'
import { deleteEntityImage, generateEntityAppearance, uploadEntityImage } from './api'

type EntityImageFieldProps = {
  entity?: HomeEntity
  invalidateQueryKey: string
  allowGenerate: boolean
  onEntityChange: (entity: HomeEntity) => void
  onAppearanceChange: (appearanceDesc: string) => void
}

export function EntityImageField({
  entity,
  invalidateQueryKey,
  allowGenerate,
  onEntityChange,
  onAppearanceChange,
}: EntityImageFieldProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [message, setMessage] = useState('')
  const [uploading, setUploading] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [generating, setGenerating] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  if (!entity) {
    return null
  }

  const refresh = (updated: HomeEntity) => {
    onEntityChange(updated)
    queryClient.invalidateQueries({ queryKey: [invalidateQueryKey] })
  }

  const handleUpload = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (!file) return
    setUploading(true)
    setMessage('')
    try {
      refresh(await uploadEntityImage(entity.id, file))
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  const handleDelete = async () => {
    setDeleting(true)
    setMessage('')
    try {
      refresh(await deleteEntityImage(entity.id))
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setDeleting(false)
    }
  }

  const handleGenerate = async () => {
    setGenerating(true)
    setMessage('')
    try {
      const updated = await generateEntityAppearance(entity.id)
      refresh(updated)
      onAppearanceChange(updated.appearance_desc ?? '')
    } catch (error) {
      setMessage((error as Error).message)
    } finally {
      setGenerating(false)
    }
  }

  return (
    <div className="entity-image-field">
      {entity.image_url ? (
        <img src={entity.image_url} alt={entity.name} className="entity-image" />
      ) : (
        <div className="entity-image entity-image-empty">{t('home_profile.no_image')}</div>
      )}
      <input
        ref={fileInputRef}
        type="file"
        accept="image/*"
        className="visually-hidden"
        onChange={handleUpload}
      />
      <div className="entity-image-actions">
        {entity.image_url ? (
          <>
            <button
              type="button"
              className="ghost"
              disabled={uploading}
              onClick={() => fileInputRef.current?.click()}
            >
              {uploading ? t('home_profile.uploading') : t('home_profile.re_upload')}
            </button>
            <button type="button" className="ghost" disabled={deleting} onClick={handleDelete}>
              {deleting ? t('home_profile.deleting') : t('home_profile.delete')}
            </button>
          </>
        ) : (
          <button
            type="button"
            className="ghost"
            disabled={uploading}
            onClick={() => fileInputRef.current?.click()}
          >
            {uploading ? t('home_profile.uploading') : t('home_profile.upload_image')}
          </button>
        )}
        {allowGenerate ? (
          <button type="button" className="ghost" disabled={generating} onClick={handleGenerate}>
            {generating ? t('home_profile.generating') : t('home_profile.generate_ai')}
          </button>
        ) : null}
      </div>
      {message ? <div className="entity-image-error">{message}</div> : null}
    </div>
  )
}
